import subprocess
import os
import logging
import shutil
from utils import validate_paths

def run_smccnet(omics_file_paths, phenotype_file_path, config, output_dir):
    """
    Execute the SmCCNet algorithm for graph generation.
    """
    logger = logging.getLogger(__name__)

    logger.info("Running SmCCNet Algorithm")

    try:
        # Extract parameters from config
        smccnet_config = config["graph_generation"]["smccnet"]
        kfold = smccnet_config["kfold"]
        summarization = smccnet_config["summarization"]
        seed = smccnet_config["seed"]
        data_type = smccnet_config["data_type"]

        # Define paths
        input_dir = os.path.abspath(config["graph_generation"]["paths"]["input_dir"])
        saving_dir = os.path.abspath(config["graph_generation"]["paths"]["output_dir"])
        script_dir = os.path.dirname(os.path.abspath(__file__))


        # Ensure saving directory exists
        os.makedirs(saving_dir, exist_ok=True)
        validate_paths(script_dir,saving_dir, *omics_file_paths, phenotype_file_path)

        # Construct the R script command
        r_script = os.path.join(script_dir, "SmCCNet.R")
        if not os.path.isfile(r_script):
            logger.error(f"R script not found: {r_script}")
            raise FileNotFoundError(f"R script not found: {r_script}")

        # Construct full paths for omics files and phenotype file
        omics_file_paths_full = [os.path.join(input_dir, f) for f in smccnet_config["omics_files"]]
        phenotype_file_path_full = os.path.join(input_dir, smccnet_config["phenotype_file"])

        command = [
            "Rscript",
            r_script,
            input_dir,  # base_path
            phenotype_file_path_full,
            ",".join(omics_file_paths_full),
            ",".join(data_type),
            str(kfold),
            summarization,
            str(seed),
            saving_dir,
        ]

        logger.debug(f"Executing command: {' '.join(command)}")

        # Execute the R script and capture output
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        logger.info("SmCCNet R script executed successfully.")
        logger.info(f"SmCCNet Output:\n{result.stdout}")
        if result.stderr:
            logger.warning(f"SmCCNet Warnings/Errors:\n{result.stderr}")

        # Call the cleanup function
        cleanup_output(saving_dir)

    except subprocess.CalledProcessError as cpe:
        logger.error(f"SmCCNet R script failed with error: {cpe.stderr}")
        raise
    except Exception as e:
        logger.error(f"Error in SmCCNet Algorithm: {e}")
        raise

def cleanup_output(saving_dir):
    """
    Cleans up and reorganizes the output files generated by SmCCNet.
    """
    logger = logging.getLogger(__name__)

    try:
        # Paths
        csv_files_dir = os.path.join(saving_dir, "CSV_Files")
        m1_output_dir = saving_dir  # m1_graph_generation/output
        m2_output_dir = os.path.abspath(os.path.join("m2_clustering", "output", "smccnet"))
        smccnet_dir = os.path.join(m1_output_dir, "smccnet")

        # Ensure m2 output directory exists
        os.makedirs(m2_output_dir, exist_ok=True)
        os.makedirs(smccnet_dir, exist_ok=True)

        # Move global_network.csv up one directory
        global_network_csv = os.path.join(csv_files_dir, "global_network.csv")
        if os.path.isfile(global_network_csv):
            shutil.move(global_network_csv, m1_output_dir)
            logger.info(f"Moved global_network.csv to {m1_output_dir}")

        # Move subnetwork CSV files to m2_clustering/output/smccnet/
        for file_name in os.listdir(csv_files_dir):
            if file_name.startswith("size_") and file_name.endswith(".csv"):
                src_file = os.path.join(csv_files_dir, file_name)
                shutil.move(src_file, m2_output_dir)
                logger.info(f"Moved {file_name} to {m2_output_dir}")

        # Delete the CSV_Files directory
        if os.path.isdir(csv_files_dir):
            shutil.rmtree(csv_files_dir)
            logger.info(f"Deleted directory {csv_files_dir}")

        # Move .Rdata files and CVResults.csv to m1_graph_generation/output/smccnet/
        for file_name in os.listdir(saving_dir):
            if file_name.endswith(".Rdata") or file_name == "CVResults.csv":
                src_file = os.path.join(saving_dir, file_name)
                shutil.move(src_file, smccnet_dir)
                logger.info(f"Moved {file_name} to {smccnet_dir}")

        logger.info("Cleanup and reorganization completed successfully.")

    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        raise
