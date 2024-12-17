import os
import re
import logging
import statistics
import tempfile
from typing import Optional, List

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import pandas as pd
import numpy as np
import networkx as nx
from torch_geometric.data import Data
from torch_geometric.transforms import RandomNodeSplit
from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler
from ray import train
from ray.train import Checkpoint

from bioneuralnet.network_embedding import GCN, GAT, SAGE, GIN


# Set up logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class DPMON:
    def __init__(
        self,
        adjacency_matrix: pd.DataFrame,
        omics_list: List[pd.DataFrame],
        phenotype_data: pd.DataFrame,  
        clinical_data: pd.DataFrame,  
        model: str,
        gnn_hidden_dim: int = 16,
        layer_num: int = 5,
        nn_hidden_dim1: int = 8,
        nn_hidden_dim2: int = 8,
        epoch_num: int = 5,
        repeat_num: int = 5,
        lr: float = 0.01,
        weight_decay: float = 1e-4,
        tune: bool = False,
        gpu: bool = False,
        cuda: int = 0,
        output_dir: Optional[str] = None
    ):
        # Mandatory checks
        if clinical_data is None or clinical_data.empty:
            raise ValueError("Clinical data (features_file) is required and cannot be empty.")

        if phenotype_data is None or phenotype_data.empty or 'finalgold_visit' not in phenotype_data.columns:
            raise ValueError("Phenotype data must contain 'finalgold_visit' and cannot be empty.")

        if not omics_list or any(df.empty for df in omics_list):
            raise ValueError("All provided omics data files must be non-empty.")

        if adjacency_matrix is None or adjacency_matrix.empty:
            raise ValueError("Adjacency matrix is required and cannot be empty.")

        # Store parameters
        self.adjacency_matrix = adjacency_matrix
        self.omics_list = omics_list
        self.phenotype_data = phenotype_data
        self.clinical_data = clinical_data
        self.model = model
        self.gnn_hidden_dim = gnn_hidden_dim
        self.layer_num = layer_num
        self.nn_hidden_dim1 = nn_hidden_dim1
        self.nn_hidden_dim2 = nn_hidden_dim2
        self.epoch_num = epoch_num
        self.repeat_num = repeat_num
        self.lr = lr
        self.weight_decay = weight_decay
        self.tune = tune
        self.gpu = gpu
        self.cuda = cuda
        self.output_dir = output_dir if output_dir else f"dpmon_output_{os.getpid()}"

        os.makedirs(self.output_dir, exist_ok=True)
        logger.info("Initialized DPMON with the provided parameters.")


    def run(self) -> pd.DataFrame:
        """
        Execute the DPMON pipeline for disease prediction.

        Steps:
        1. **Combining Omics and Phenotype Data**: Merges the provided omics datasets and ensures that  the phenotype (finalgold_visit) column is included.
        2. **Tuning or Training**: 
            - If tune=True, performs hyperparameter tuning using Ray Tune and returns an empty DataFrame.
            - If tune=False, runs standard training to generate predictions.
        3. **Predictions**: If training is performed, returns a DataFrame of predictions with 'Actual' and 'Predicted' columns.

        Returns:
            pd.DataFrame:
                If tune=False, a DataFrame containing disease phenotype predictions for each sample.
                If tune=True, returns an empty DataFrame since no predictions are generated.

        Raises:
            ValueError: If required inputs (e.g., clinical data) are missing or invalid.
            Exception: For any unforeseen issues during the pipeline execution.

        Notes:
            - DPMON relies on internally-generated embeddings (via GNNs), node correlations, and a downstream neural network.
            - Ensure that the adjacency_matrix and omics data are properly aligned and that clinical/phenotype data match the sample indices.
        """
        logger.info("Starting DPMON run.")

        dpmon_params = {
            'model': self.model,
            'gnn_hidden_dim': self.gnn_hidden_dim,
            'layer_num': self.layer_num,
            'nn_hidden_dim1': self.nn_hidden_dim1,
            'nn_hidden_dim2': self.nn_hidden_dim2,
            'epoch_num': self.epoch_num,
            'repeat_num': self.repeat_num,
            'lr': self.lr,
            'weight_decay': self.weight_decay,
            'gpu': self.gpu,
            'cuda': self.cuda,
            'tune': self.tune
        }

        # Combine all omics data and phenotype
        combined_omics = pd.concat(self.omics_list, axis=1)
        if 'finalgold_visit' not in combined_omics.columns:
            # Merge phenotype
            combined_omics = combined_omics.merge(self.phenotype_data[['finalgold_visit']], left_index=True, right_index=True)

        if self.tune:
            logger.info("Running hyperparameter tuning for DPMON.")
            run_hyperparameter_tuning(
                dpmon_params,
                self.adjacency_matrix,
                combined_omics,
                self.clinical_data
            )
            return pd.DataFrame()

        # Otherwise, run standard training and return predictions
        logger.info("Running standard training for DPMON.")
        predictions = run_standard_training(
            dpmon_params,
            self.adjacency_matrix,
            combined_omics,
            self.clinical_data,
            output_dir=self.output_dir
        )

        logger.info("DPMON run completed.")
        return predictions


def setup_device(gpu, cuda):
    if gpu:
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ['CUDA_VISIBLE_DEVICES'] = str(cuda)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        if torch.cuda.is_available():
            logger.info(f'Using GPU {cuda}')
        else:
            logger.warning(f'GPU {cuda} requested but not available, using CPU')
    else:
        device = torch.device('cpu')
        logger.info('Using CPU')
    return device


def slice_omics_datasets(omics_dataset: pd.DataFrame, adjacency_matrix: pd.DataFrame) -> List[pd.DataFrame]:
    logger.info("Slicing omics dataset based on network nodes.")
    omics_network_nodes_names = adjacency_matrix.index.tolist()

    # Clean omics dataset columns
    clean_columns = []
    for node in omics_dataset.columns:
        node_clean = re.sub(r'[^0-9a-zA-Z_]', '.', node)
        if not node_clean[0].isalpha():
            node_clean = 'X' + node_clean
        clean_columns.append(node_clean)
    omics_dataset.columns = clean_columns

    missing_nodes = set(omics_network_nodes_names) - set(omics_dataset.columns)
    if missing_nodes:
        logger.error(f"Nodes missing in omics data: {missing_nodes}")
        raise ValueError("Missing nodes in omics dataset.")

    selected_columns = omics_network_nodes_names + ['finalgold_visit']
    return [omics_dataset[selected_columns]]


def build_omics_networks_tg(adjacency_matrix: pd.DataFrame, omics_datasets: List[pd.DataFrame], clinical_data: pd.DataFrame) -> List[Data]:
    logger.info("Building PyTorch Geometric Data object from adjacency matrix.")
    omics_network_nodes_names = adjacency_matrix.index.tolist()

    # Create NetworkX graph
    G = nx.from_pandas_adjacency(adjacency_matrix)
    node_mapping = {node_name: idx for idx, node_name in enumerate(omics_network_nodes_names)}
    G = nx.relabel_nodes(G, node_mapping)
    num_nodes = len(node_mapping)
    logger.info(f"Number of nodes in network: {num_nodes}")

    # Use clinical_data to compute correlations
    if clinical_data is not None and not clinical_data.empty:
        clinical_vars = clinical_data.columns.tolist()
        logger.info(f"Using clinical vars for node features: {clinical_vars}")
        omics_dataset = omics_datasets[0]
        missing_nodes = set(omics_network_nodes_names) - set(omics_dataset.columns)
        if missing_nodes:
            raise ValueError("Missing nodes for correlation computation.")
        node_features = []
        for node_name in omics_network_nodes_names:
            correlations = []
            for var in clinical_vars:
                corr_value = abs(omics_dataset[node_name].corr(clinical_data[var].astype('float64')))
                correlations.append(corr_value)
            node_features.append(correlations)
        x = torch.tensor(node_features, dtype=torch.float)
    else:
        x = torch.randn((num_nodes, 10), dtype=torch.float)
        logger.info("No clinical data provided or empty. Using random features.")

    edge_index = torch.tensor(list(G.edges()), dtype=torch.long).t().contiguous()
    # Duplicate edges for undirected graph
    edge_index = torch.cat([edge_index, edge_index.flip(0)], dim=1)

    edge_weight = torch.tensor([
        data.get('weight', 1.0) for _, _, data in G.edges(data=True)
    ], dtype=torch.float)
    # Duplicate edge weights
    edge_weight = torch.cat([edge_weight, edge_weight], dim=0)

    data = Data(x=x, edge_index=edge_index, edge_attr=edge_weight)
    num_val_nodes = 10 if num_nodes >= 30 else 5
    num_test_nodes = 12 if num_nodes >= 30 else 5
    transform = RandomNodeSplit(num_val=num_val_nodes, num_test=num_test_nodes)
    data = transform(data)

    return [data]


def run_standard_training(dpmon_params, adjacency_matrix, combined_omics, clinical_data, output_dir):
    device = setup_device(dpmon_params['gpu'], dpmon_params['cuda'])

    # Slice omics dataset
    omics_dataset = slice_omics_datasets(combined_omics, adjacency_matrix)
    # Build network
    omics_networks_tg = build_omics_networks_tg(adjacency_matrix, omics_dataset, clinical_data)

    accuracies = []
    last_predictions_df = pd.DataFrame()

    for omics_data, omics_network in zip(omics_dataset, omics_networks_tg):
        for i in range(dpmon_params['repeat_num']):
            logger.info(f"Training iteration {i+1}/{dpmon_params['repeat_num']}")
            model = NeuralNetwork(
                model_type=dpmon_params['model'],
                gnn_input_dim=omics_network.x.shape[1],
                gnn_hidden_dim=dpmon_params['gnn_hidden_dim'],
                gnn_layer_num=dpmon_params['layer_num'],
                ae_encoding_dim=1,
                nn_input_dim=omics_data.drop(['finalgold_visit'], axis=1).shape[1],
                nn_hidden_dim1=dpmon_params['nn_hidden_dim1'],
                nn_hidden_dim2=dpmon_params['nn_hidden_dim2'],
                nn_output_dim=omics_data['finalgold_visit'].nunique()
            ).to(device)

            criterion = nn.CrossEntropyLoss()
            optimizer = optim.Adam(model.parameters(), lr=dpmon_params['lr'], weight_decay=dpmon_params['weight_decay'])

            train_features = torch.FloatTensor(omics_data.drop(['finalgold_visit'], axis=1).values).to(device)
            train_labels = {
                'labels': torch.LongTensor(omics_data['finalgold_visit'].values.copy()).to(device),
                'omics_network': omics_network.to(device)
            }

            accuracy = train_model(model, criterion, optimizer, train_features, train_labels, dpmon_params['epoch_num'])
            accuracies.append(accuracy)

            # Save model
            model_path = os.path.join(output_dir, f'dpm_model_iter_{i+1}.pth')
            torch.save(model.state_dict(), model_path)
            logger.info(f"Model saved to {model_path}")

            # Save predictions
            model.eval()
            with torch.no_grad():
                predictions, _ = model(train_features, omics_network.to(device))
                _, predicted = torch.max(predictions, 1)
                predictions_path = os.path.join(output_dir, f'predictions_iter_{i+1}.csv')
                predictions_df = pd.DataFrame({
                    'Actual': omics_data['finalgold_visit'].values,
                    'Predicted': predicted.cpu().numpy()
                })
                predictions_df.to_csv(predictions_path, index=False)
                logger.info(f"Predictions saved to {predictions_path}")
                last_predictions_df = predictions_df

    if accuracies:
        max_accuracy = max(accuracies)
        avg_accuracy = sum(accuracies) / len(accuracies)
        std_accuracy = statistics.stdev(accuracies) if len(accuracies) > 1 else 0.0
        logger.info(f"Best Accuracy: {max_accuracy:.4f}")
        logger.info(f"Average Accuracy: {avg_accuracy:.4f}")
        logger.info(f"Standard Deviation of Accuracy: {std_accuracy:.4f}")

    return last_predictions_df


def run_hyperparameter_tuning(dpmon_params, adjacency_matrix, combined_omics, clinical_data):
    device = setup_device(dpmon_params['gpu'], dpmon_params['cuda'])

    omics_dataset = slice_omics_datasets(combined_omics, adjacency_matrix)
    omics_networks_tg = build_omics_networks_tg(adjacency_matrix, omics_dataset, clinical_data)

    pipeline_configs = {
        # 'gnn_layer_num': tune.choice([2, 4, 8, 16, 32, 64, 128]),
        # 'gnn_hidden_dim': tune.choice([4, 8, 16, 32, 64, 128]),
        # 'lr': tune.loguniform(1e-4, 1e-1),
        # 'weight_decay': tune.loguniform(1e-4, 1e-1),
        # 'nn_hidden_dim1': tune.choice([4, 8, 16, 32, 64, 128]),
        # 'nn_hidden_dim2': tune.choice([4, 8, 16, 32, 64, 128]),
        # 'num_epochs': tune.choice([2, 16, 64, 512, 1024, 4096, 8192]),

        # Reduced search space for faster tuning (Testing Olnly)
        'gnn_layer_num': tune.choice([2, 4, 8]),
        'gnn_hidden_dim': tune.choice([4, 8, 16]),
        'lr': tune.loguniform(1e-4, 1e-1),
        'weight_decay': tune.loguniform(1e-4, 1e-1),
        'nn_hidden_dim1': tune.choice([4, 8, 16]),
        'nn_hidden_dim2': tune.choice([4, 8, 16]),
        'num_epochs': tune.choice([2, 16, 64]),
    }

    reporter = CLIReporter(metric_columns=["loss", "accuracy", "training_iteration"])
    scheduler = ASHAScheduler(metric="loss", mode="min", grace_period=10, reduction_factor=2)
    gpu_resources = 1 if dpmon_params['gpu'] else 0

    for omics_data, omics_network_tg in zip(omics_dataset, omics_networks_tg):
        os.environ['TUNE_DISABLE_STRICT_METRIC_CHECKING'] = '1'
        logger.info(f"Starting hyperparameter tuning for dataset shape: {omics_data.shape}")

        def tune_train_n(config):
            model = NeuralNetwork(
                model_type=dpmon_params['model'],
                gnn_input_dim=omics_network_tg.x.shape[1],
                gnn_hidden_dim=config['gnn_hidden_dim'],
                gnn_layer_num=config['gnn_layer_num'],
                ae_encoding_dim=1,
                nn_input_dim=omics_data.drop(['finalgold_visit'], axis=1).shape[1],
                nn_hidden_dim1=config['nn_hidden_dim1'],
                nn_hidden_dim2=config['nn_hidden_dim2'],
                nn_output_dim=omics_data['finalgold_visit'].nunique()
            ).to(device)

            criterion = nn.CrossEntropyLoss()
            optimizer = optim.Adam(model.parameters(), lr=config['lr'], weight_decay=config['weight_decay'])

            train_features = torch.FloatTensor(omics_data.drop(['finalgold_visit'], axis=1).values).to(device)
            train_labels = {
                'labels': torch.LongTensor(omics_data['finalgold_visit'].values.copy()).to(device),
                'omics_network': omics_network_tg.to(device)
            }

            for epoch in range(config['num_epochs']):
                model.train()
                optimizer.zero_grad()
                outputs, _ = model(train_features, train_labels['omics_network'])
                loss = criterion(outputs, train_labels['labels'])
                loss.backward()
                optimizer.step()

                _, predicted = torch.max(outputs, 1)
                total = train_labels['labels'].size(0)
                correct = (predicted == train_labels['labels']).sum().item()
                accuracy = correct / total

                metrics = {"loss": loss.item(), "accuracy": accuracy}
                with tempfile.TemporaryDirectory() as tempdir:
                    torch.save(
                        {"epoch": epoch, "model_state": model.state_dict()},
                        os.path.join(tempdir, "checkpoint.pt"),
                    )
                    train.report(metrics=metrics, checkpoint=Checkpoint.from_directory(tempdir))

            # Final evaluation
            model.eval()
            with torch.no_grad():
                outputs, _ = model(train_features, train_labels['omics_network'])
                loss = criterion(outputs, train_labels['labels'])
                _, predicted = torch.max(outputs, 1)
                total = train_labels['labels'].size(0)
                correct = (predicted == train_labels['labels']).sum().item()
                accuracy = correct / total
                metrics = {"loss": loss.item(), "accuracy": accuracy}
                with tempfile.TemporaryDirectory() as tempdir:
                    torch.save(
                        {"epoch": config['num_epochs'], "model_state": model.state_dict()},
                        os.path.join(tempdir, "checkpoint.pt"),
                    )
                    train.report(metrics=metrics, checkpoint=Checkpoint.from_directory(tempdir))

        result = tune.run(
            tune_train_n,
            resources_per_trial={"cpu": 2, "gpu": gpu_resources},
            config=pipeline_configs,
            num_samples=10,
            scheduler=scheduler,
            name='Hyperparameter_Tuning',
            progress_reporter=reporter,
            keep_checkpoints_num=1,
            checkpoint_score_attr='min-loss'
        )

        best_trial = result.get_best_trial("loss", "min", "last")
        logger.info("Best trial config: {}".format(best_trial.config))
        logger.info("Best trial final loss: {}".format(best_trial.last_result["loss"]))
        logger.info("Best trial final accuracy: {}".format(best_trial.last_result["accuracy"]))


def train_model(model, criterion, optimizer, train_data, train_labels, epoch_num):
    model.train()
    for epoch in range(epoch_num):
        optimizer.zero_grad()
        outputs, _ = model(train_data, train_labels['omics_network'])
        loss = criterion(outputs, train_labels['labels'])
        loss.backward()
        optimizer.step()

        if (epoch + 1) % 10 == 0 or epoch == 0:
            logger.info(f"Epoch [{epoch+1}/{epoch_num}], Loss: {loss.item():.4f}")

    # Evaluation after training
    model.eval()
    with torch.no_grad():
        predictions, _ = model(train_data, train_labels['omics_network'])
        _, predicted = torch.max(predictions, 1)
        accuracy = (predicted == train_labels['labels']).sum().item() / len(train_labels['labels'])
        logger.info(f"Training Accuracy: {accuracy:.4f}")

    return accuracy


class NeuralNetwork(nn.Module):
    def __init__(self,
                 model_type,
                 gnn_input_dim,
                 gnn_hidden_dim,
                 gnn_layer_num,
                 ae_encoding_dim,
                 nn_input_dim,
                 nn_hidden_dim1,
                 nn_hidden_dim2,
                 nn_output_dim):
        super(NeuralNetwork, self).__init__()

        if model_type == 'GCN':
            self.gnn = GCN(input_dim=gnn_input_dim, hidden_dim=gnn_hidden_dim, layer_num=gnn_layer_num)
        elif model_type == 'GAT':
            self.gnn = GAT(input_dim=gnn_input_dim, hidden_dim=gnn_hidden_dim, layer_num=gnn_layer_num)
        elif model_type == 'SAGE':
            self.gnn = SAGE(input_dim=gnn_input_dim, hidden_dim=gnn_hidden_dim, output_dim=gnn_hidden_dim, layer_num=gnn_layer_num)
        elif model_type == 'GIN':
            self.gnn = GIN(input_dim=gnn_input_dim, hidden_dim=gnn_hidden_dim, output_dim=gnn_hidden_dim, layer_num=gnn_layer_num)
        else:
            raise ValueError(f"Unsupported GNN model type: {model_type}")

        self.autoencoder = Autoencoder(input_dim=gnn_hidden_dim, encoding_dim=ae_encoding_dim)
        self.dim_averaging = DimensionAveraging()
        self.predictor = DownstreamTaskNN(nn_input_dim, nn_hidden_dim1, nn_hidden_dim2, nn_output_dim)

    def forward(self, omics_dataset, omics_network_tg):
        omics_network_nodes_embedding = self.gnn(omics_network_tg)
        omics_network_nodes_embedding_ae = self.autoencoder(omics_network_nodes_embedding)
        omics_network_nodes_embedding_avg = self.dim_averaging(omics_network_nodes_embedding_ae)
        omics_dataset_with_embeddings = torch.mul(
            omics_dataset,
            omics_network_nodes_embedding_avg.expand(omics_dataset.shape[1], omics_dataset.shape[0]).t()
        )
        predictions = self.predictor(omics_dataset_with_embeddings)
        return predictions, omics_dataset_with_embeddings


class Autoencoder(nn.Module):
    def __init__(self, input_dim, encoding_dim):
        super(Autoencoder, self).__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 8),
            nn.ReLU(),
            nn.Linear(8, 4),
            nn.ReLU(),
            nn.Linear(4, encoding_dim)
        )
        self.decoder = nn.Sequential(
            nn.Linear(encoding_dim, 4),
            nn.ReLU(),
            nn.Linear(4, 8),
            nn.ReLU(),
            nn.Linear(8, input_dim)
        )

    def forward(self, x):
        x = self.encoder(x)
        return x


class DimensionAveraging(nn.Module):
    def __init__(self):
        super(DimensionAveraging, self).__init__()

    def forward(self, x):
        return torch.mean(x, dim=1, keepdim=True)


class DownstreamTaskNN(nn.Module):
    def __init__(self, input_size, hidden_dim1, hidden_dim2, output_dim):
        super(DownstreamTaskNN, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_dim1)
        self.bn1 = nn.BatchNorm1d(hidden_dim1)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Linear(hidden_dim1, hidden_dim2)
        self.bn2 = nn.BatchNorm1d(hidden_dim2)
        self.relu2 = nn.ReLU()
        self.fc3 = nn.Linear(hidden_dim2, output_dim)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, x):
        x = self.fc1(x)
        x = self.bn1(x)
        x = self.relu1(x)
        x = self.fc2(x)
        x = self.bn2(x)
        x = self.relu2(x)
        x = self.fc3(x)
        x = self.softmax(x)
        return x
