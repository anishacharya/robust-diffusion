conda activate robust
# Get and Preprocess raw data

# ----------
## CIFAR-10
# ----------
wget https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz
python3 dataset_tool.py --source=cifar-10-python.tar.gz --dest=datasets/cifar10-32x32.zip
rm -rf cifar-10-python.tar.gz