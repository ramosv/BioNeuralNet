import os
import subprocess
import pandas as pd
from typing import List, Dict, Any
from ..utils.logger import get_logger
import json
from io import StringIO

class SmCCNet:
    """
    SmCCNet Class for Graph Generation using Sparse Multiple Canonical Correlation Networks (SmCCNet).

    This class handles the preprocessing of omics data, execution of the SmCCNet R script,
    and retrieval of the resulting adjacency matrix, all using in-memory data structures.
    """

    def __init__(
        self,
        phenotype_df: pd.DataFrame,
        omics_dfs: List[pd.DataFrame],
        data_types: List[str],
        kfold: int = 5,
        summarization: str = "PCA",
        seed: int = 732,
    ):
        """
        Initializes the SmCCNet instance.

        Args:
            phenotype_df (pd.DataFrame): DataFrame containing phenotype data. The first column should be sample IDs.
            omics_dfs (List[pd.DataFrame]): List of DataFrames, each representing an omics dataset. Each DataFrame should have sample IDs as the first column.
            data_types (List[str]): List of omics data types (e.g., ["protein", "metabolite"]).
            kfold (int, optional): Number of folds for cross-validation. Defaults to 5.
            summarization (str, optional): Summarization method. Defaults to "PCA".
            seed (int, optional): Random seed for reproducibility. Defaults to 732.
        """
        # Assign parameters
        self.phenotype_df = phenotype_df
        self.omics_dfs = omics_dfs
        self.data_types = data_types
        self.kfold = kfold
        self.summarization = summarization
        self.seed = seed

        # Initialize logger
        self.logger = get_logger(__name__)
        self.logger.info("Initialized SmCCNet with the following parameters:")
        self.logger.info(f"K-Fold: {self.kfold}")
        self.logger.info(f"Summarization: {self.summarization}")
        self.logger.info(f"Seed: {self.seed}")

        # Validate inputs
        if len(self.omics_dfs) != len(self.data_types):
            self.logger.error("Number of omics dataframes does not match number of data types.")
            raise ValueError("Number of omics dataframes does not match number of data types.")

    def preprocess_data(self) -> Dict[str, Any]:
        """
        Preprocesses the omics data to ensure alignment and handle missing values.

        Returns:
            Dict[str, Any]: Dictionary containing serialized phenotype and omics data.
        """
        self.logger.info("Preprocessing omics data for NaN or infinite values.")

        # Ensure that the first column is sample IDs
        phenotype_ids = self.phenotype_df.iloc[:, 0]
        self.logger.info(f"Number of samples in phenotype data: {len(phenotype_ids)}")

        # Initialize a boolean Series to track valid samples
        valid_samples = pd.Series([True] * len(phenotype_ids), index=self.phenotype_df.index)

        serialized_data = {
            'phenotype': self.phenotype_df.to_csv(index=False)
        }

        for idx, omics_df in enumerate(self.omics_dfs):
            data_type = self.data_types[idx]
            self.logger.info(f"Processing omics DataFrame {idx+1}/{len(self.omics_dfs)}: Data Type = {data_type}")

            # Ensure sample IDs align with phenotype data
            omics_ids = omics_df.iloc[:, 0]
            if not omics_ids.equals(phenotype_ids):
                self.logger.warning(f"Sample IDs in omics dataframe {idx+1} do not match phenotype data. Aligning data.")
                omics_df = omics_df.set_index(omics_ids).loc[phenotype_ids].reset_index()

            # Check for NaN values
            if omics_df.isnull().values.any():
                self.logger.warning(f"NaN values detected in omics dataframe {idx+1}. Marking samples with NaNs as invalid.")
                valid_samples &= ~omics_df.isnull().any(axis=1)

            # Check for infinite values
            if (omics_df == float('inf')).any().any() or (omics_df == -float('inf')).any().any():
                self.logger.warning(f"Infinite values detected in omics dataframe {idx+1}. Replacing with NaN and marking samples as invalid.")
                omics_df.replace([float('inf'), -float('inf')], pd.NA, inplace=True)
                valid_samples &= ~omics_df.isnull().any(axis=1)

            # Serialize omics dataframe
            serialized_data[f'omics_{idx+1}'] = omics_df.to_csv(index=False)

        self.logger.info("Preprocessing completed successfully.")
        return serialized_data

    def run_smccnet(self, serialized_data: Dict[str, Any]) -> str:
        """
        Executes the SmCCNet R script by passing serialized data via standard input.

        Args:
            serialized_data (Dict[str, Any]): Dictionary containing serialized phenotype and omics data.

        Returns:
            str: Serialized adjacency matrix JSON string from R script.
        """
        try:
            self.logger.info("Preparing data for SmCCNet R script.")

            # Combine serialized data into a JSON string
            json_data = json.dumps(serialized_data)

            # Define the R script path
            script_dir = os.path.dirname(os.path.abspath(__file__))
            r_script = os.path.join(script_dir, "SmCCNet.R")

            if not os.path.isfile(r_script):
                self.logger.error(f"R script not found: {r_script}")
                raise FileNotFoundError(f"R script not found: {r_script}")

            # Define command arguments
            command = [
                "Rscript",
                r_script,
                ','.join(self.data_types),  # Pass data_types directly
                str(self.kfold),
                self.summarization,
                str(self.seed)
            ]

            self.logger.debug(f"Executing command: {' '.join(command)}")

            # Execute the R script, passing JSON data via stdin
            result = subprocess.run(
                command,
                input=json_data,
                text=True,
                capture_output=True,
                check=True
            )

            self.logger.info("SmCCNet R script executed successfully.")
            self.logger.debug(f"SmCCNet Output:\n{result.stdout}")

            # Check for warnings or errors
            if result.stderr:
                self.logger.warning(f"SmCCNet Warnings/Errors:\n{result.stderr}")

            # The R script should output the adjacency matrix as a JSON string
            adjacency_json = result.stdout.strip()

            return adjacency_json

        except subprocess.CalledProcessError as e:
            self.logger.error(f"R script execution failed: {e.stderr}")
            raise
        except Exception as e:
            self.logger.error(f"Error during SmCCNet execution: {e}")
            raise

    def run(self) -> pd.DataFrame:
        """
        Executes the entire SmCCNet workflow.

        Returns:
            pd.DataFrame: Adjacency matrix representing the global network.
        """
        try:
            self.logger.info("Starting SmCCNet Graph Generation Workflow.")

            # Preprocess data
            serialized_data = self.preprocess_data()

            # Run SmCCNet R script
            adjacency_json = self.run_smccnet(serialized_data)

            # Deserialize adjacency matrix from JSON
            adjacency_matrix = pd.read_json(StringIO(adjacency_json), orient='split')
            self.logger.info("Adjacency matrix deserialized successfully.")

            self.logger.info("SmCCNet Graph Generation completed successfully.")
            return adjacency_matrix

        except Exception as e:
            self.logger.error(f"Error in SmCCNet Graph Generation: {e}")
            raise
