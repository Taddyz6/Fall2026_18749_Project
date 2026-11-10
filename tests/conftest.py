import os

# Command-line processes started by tests should not append to logs/console/.
os.environ.setdefault("FT_LOG_DIR", "off")
