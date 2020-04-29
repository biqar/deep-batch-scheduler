# deep-batch-scheduler
This repo includes the deep batch scheduler source code and necessary datasets to run the experiments/tests. 

The code has been tested on Ubuntu 18.04/16.04 with Tensorflow 1.14 and SpinningUp 0.2. Newer version of Tensorflow (such as 2.x) does not work because of the new APIs. Windows 10 should be OK to run the code, only the installation of the dependencies (such as Gym environment and SpinningUp could be bumpy).

## Installation

### Required Software
* Python 3.7
```bash
sudo apt-get install software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install python3.7
```
* OpenMPI 
```bash
sudo apt-get install openmpi-bin openmpi-doc libopenmpi-dev
```

* Tensorflow
```bash
sudo apt install python3.7-dev python3-pip
sudo pip3 install -U virtualenv
virtualenv --system-site-packages -p python3.7 ./venv
source ./venv/bin/activate  # sh, bash, ksh, or zsh
pip install --upgrade pip
pip install --upgrade tensorflow==1.14
```

### Install GYM

```bash
git clone https://github.com/openai/gym.git
pip install -e ./gym
```

### Install SpinningUp
```bash
git clone https://github.com/openai/spinningup.git
pip install -e ./spinningup
```

### Install Deep Batch Scheduler
```bash
git clone https://github.com/DIR-LAB/deep-batch-scheduler.git
```

### File Structure

```
data/: Contains a series of workload and real-world traces.
cluster.py: Contains Machine and Cluster classes.
job.py: Contains Job and Workloads classed. 
compare-pick-jobs.py: Test training results and compare it with different policies.
HPCSimPickJobs.py: SchedGym Environment.
ppo-pick-jobs.py: Train RLScheduler using PPO algorithm.
```

To change the hyper-parameters, such as `MAX_OBSV_SIZE` or the trajectory length during training, you can change them in HPCSimPickJobs.py. You can also change to different neural networks (MLP and LeNet) in HPCSimPickJob.py. More details about the changes will be offered as standalone wiki page.

### Training
To train a RL model based on a job trace, run this command:
```bash
python ppo-pick-jobs.py --workload "./data/lublin_256.swf" --exp_name your-exp-name --trajs 500 --seed 0
```
There are many other parameters in the source file.
* `--model`, specify a saved trained model (for two-step training and re-training)
* `--pre_trained`, specify whether this trainig will be a twp-step training or re-training
* `--score_type`, specify which scheduling metrics you are optimizing for: [0]：bounded job slowdown；[1]: job waiting time; [2]: job response time; [3] system resource utilization.

### Monitor Training 

After running Default Training, a folder named `logs/your-exp-name/` will be generated under `./data`. 

```bash
python spinningup/spinup/utils/plot.py ./data/logs/your-exp-name/
```

It will plot the training curve.

### Test and Compare

After RLScheduler converges, you can test the result and compare it with different policies such as FCFS, SJF, WFP3, UNICEP, and F1.

```bash
python compare-pick-jobs.py --rlmodel "./data/logs/your-exp-name/your-exp-name_s0/" --workload "./data/lublin_256.swf --len 2048 --iter 10"
```
There are many parameters you can use:
* `--seed`, the seed for random sampling
* `--iter`, how many iterations for the testing
* `--backfil`, enable/disable backfilling during the test
* `--score_type`, specify the scheduling metrics. [0]：bounded job slowdown；[1]: job waiting time; [2]: job response time; [3] system resource utilization.
