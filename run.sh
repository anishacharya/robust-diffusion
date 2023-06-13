conda env create -f environment.yml -n robust_diffusion
conda activate robust_diffusion

# Get and Preprocess raw data
## CIFAR-10
wget https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz
python3 dataset_tool.py --source=cifar-10-python.tar.gz --dest=datasets/cifar10-32x32.zip
rm -rf cifar-10-python.tar.gz

# train a diffusion model
python3 train.py --outdir=./results/ --data=./datasets/cifar10-32x32.zip \
--batch 1 --tick 1 --max_size=10 --duration=0.01
