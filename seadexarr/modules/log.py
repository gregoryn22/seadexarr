import logging
import math
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler

import colorlog


def setup_logger(
    log_level,
    log_dir="logs",
    log_name="SeaDexArr",
    max_logs=9,
):
    """
    Set up the logger.

    Parameters:
        log_level (str): The log level to use
        log_dir (str): Directory for log files.
            Defaults to "logs"
        log_name (str): The name of the log file.
            Defaults to "SeaDexArr"
        max_logs (int): Maximum number of log files to keep.
            Defaults to 9

    Returns:
        A logger object for logging messages.
    """

    if os.environ.get("DOCKER_ENV"):
        config_dir = os.environ.get("CONFIG_DIR", "/config")
        log_dir = os.path.join(config_dir, log_dir)
    else:
        log_dir = os.path.join(os.getcwd(), log_dir)

    # Create the log directory if it doesn't exist
    # mode=0o755 ensures the directory is traversable via SMB/NFS
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, mode=0o755)

    # Define the log file path
    log_file = os.path.join(log_dir, f"{log_name}.log")

    # Check if a log file already exists. Copy, then remove to avoid I/O errors
    if os.path.isfile(log_file):
        for i in range(max_logs - 1, 0, -1):
            old_log = os.path.join(f"{log_dir}", f"{log_name}.log.{i}")
            new_log = os.path.join(f"{log_dir}", f"{log_name}.log.{i + 1}")
            if os.path.exists(old_log):
                if os.path.exists(new_log):
                    os.remove(new_log)
                shutil.copy(old_log, new_log)
                os.remove(old_log)

        shutil.copy(log_file, os.path.join(log_dir, f"{log_name}.log.1"))
        os.remove(log_file)

    # Create a logger object with the script name
    logger = logging.getLogger(log_name)
    logger.propagate = False

    # Set the log level based on the provided parameter
    log_level = log_level.upper()
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "CRITICAL": logging.CRITICAL,
    }
    numeric_level = level_map.get(log_level)

    # Define the log message format for the log files
    logfile_formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s: %(message)s", datefmt="%m/%d/%y %I:%M %p"
    )

    # Create a RotatingFileHandler for log files
    handler = RotatingFileHandler(
        log_file, mode="w", encoding="utf-8", backupCount=max_logs
    )
    handler.setFormatter(logfile_formatter)

    # Configure console logging
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        colorlog.ColoredFormatter("%(log_color)s%(levelname)s: %(message)s")
    )

    # Replace any handlers from a previous setup_logger call
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.addHandler(console_handler)

    if numeric_level is None:
        logger.setLevel(logging.INFO)
        logger.critical(f"Invalid log level '{log_level}', defaulting to 'INFO'")
    else:
        logger.setLevel(numeric_level)
        handler.setLevel(numeric_level)
        console_handler.setLevel(numeric_level)

    return logger


def centred_string(
    str_to_centre,
    total_length=80,
    str_prefix="",
):
    """Centre string for a logger

    Args:
        str_to_centre: String to centre
        total_length: Total length of the string. Defaults to 80.
        str_prefix: Will include this at the start of any string. Defaults to ""
    """

    remaining_length = total_length - len(str_to_centre)
    left_side_length = math.floor(remaining_length / 2)
    right_side_length = remaining_length - left_side_length

    return f"{str_prefix}|{' ' * left_side_length} {str_to_centre} {' ' * right_side_length}|"


def left_aligned_string(
    str_to_align,
    total_length=80,
    str_prefix="",
):
    """Left-align string for a logger

    Args:
        str_to_align: String to align
        total_length: Total length of the string
        str_prefix: Will include this at the start of any string. Defaults to ""
    """

    remaining_length = total_length - len(str_to_align)
    left_side_length = 1
    right_side_length = remaining_length - left_side_length

    return f"{str_prefix}|{' ' * left_side_length} {str_to_align} {' ' * right_side_length}|"
