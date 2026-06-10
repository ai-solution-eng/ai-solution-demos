set -e
python -m pip install --upgrade pip wheel
python -m pip install --no-cache-dir "setuptools==70.3.0"
python -c "import pkg_resources; print(pkg_resources.__name__)"