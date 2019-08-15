from job import Job, Workloads
from cluster import Cluster

import os
import math
import json
import time
import sys
import random
from random import shuffle

import numpy as np
import tensorflow as tf
import scipy.signal

import gym
from gym import spaces
from gym.spaces import Box, Discrete
from gym.utils import seeding

MAX_QUEUE_SIZE = 36
MLP_SIZE = 256

MAX_WAIT_TIME = 12 * 60 * 60 # assume maximal wait time is 12 hours.
MAX_RUN_TIME = 12 * 60 * 60 # assume maximal runtime is 12 hours

# each job has three features: wait_time, requested_node, runtime, machine states,
JOB_FEATURES = 4
DEBUG = False

JOB_SEQUENCE_SIZE = 32
PENALTY_JOB_ID = -(32 * 10)

total_epoch = 0

def combined_shape(length, shape=None):
    if shape is None:
        return (length,)
    return (length, shape) if np.isscalar(shape) else (length, *shape)

def placeholder(dim=None):
    return tf.placeholder(dtype=tf.float32, shape=combined_shape(None,dim))

def placeholders(*args):
    return [placeholder(dim) for dim in args]

def placeholder_from_space(space):
    if isinstance(space, Box):
        return placeholder(space.shape)
    elif isinstance(space, Discrete):
        return tf.placeholder(dtype=tf.int32, shape=(None,))
    raise NotImplementedError

def placeholders_from_spaces(*args):
    return [placeholder_from_space(space) for space in args]

def get_vars(scope=''):
    return [x for x in tf.trainable_variables() if scope in x.name]

def count_vars(scope=''):
    v = get_vars(scope)
    return sum([np.prod(var.shape.as_list()) for var in v])

def discount_cumsum(x, discount):
    return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]


class HPCEnv(gym.Env):
    def __init__(self):  # do nothing and return. A workaround for passing parameters to the environment
        super(HPCEnv, self).__init__()
        print("Initialize Simple HPC Env")

        self.action_space = spaces.Discrete(MAX_QUEUE_SIZE)
        self.observation_space = spaces.Box(low=0.0, high=1.0,
                                            shape=(JOB_FEATURES * MAX_QUEUE_SIZE,),
                                            dtype=np.float32)

        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []

        self.current_timestamp = 0
        self.start = 0
        self.next_arriving_job_idx = 0
        self.last_job_in_batch = 0
        self.num_job_in_batch = 0
        self.start_idx_last_reset = 0

        self.loads = None
        self.cluster = None

        self.bsld_algo_dict = {}
        self.total_epoch = 0

        self.scheduled_rl = {}
        self.penalty = 0
        self.scheduled_f1 = {}

        self.enable_preworkloads = False

    def my_init(self, workload_file = '', sched_file = ''):
        print ("loading workloads from dataset:", workload_file)
        self.loads = Workloads(workload_file)
        self.cluster = Cluster("Cluster", self.loads.max_nodes, self.loads.max_procs/self.loads.max_nodes)
        self.penalty_job_score = JOB_SEQUENCE_SIZE * self.loads.max_exec_time / 10

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]
    
    def f1_score(self, job):
        submit_time = job.submit_time
        request_processors = job.request_number_of_processors
        request_time = job.request_time
        # run_time = job.run_time
        assert job.job_id != 0
        return (np.log10(request_processors) * request_time + 870 * np.log10(submit_time))

    def f2_score(self, job):
        submit_time = job.submit_time
        request_processors = job.request_number_of_processors
        request_time = job.request_time
        # run_time = job.run_time
        assert job.job_id != 0
        # f2: r^(1/2)*n + 25600 * log10(s)
        return (np.sqrt(request_time) * request_processors + 25600 * np.log10(submit_time))

    def sjf_score(self, job):
        # run_time = job.run_time
        request_time = job.request_time
        assert job.job_id != 0
        return request_time
    
    def smallest_score(self, job):
        request_processors = job.request_number_of_processors
        assert job.job_id != 0
        return request_processors

    def fcfs_score(self, job):
        submit_time = job.submit_time
        assert job.job_id != 0
        return submit_time

    def reset(self):
        self.cluster.reset()
        self.loads.reset()

        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []

        self.current_timestamp = 0
        self.start = 0
        self.next_arriving_job_idx = 0
        self.last_job_in_batch = 0
        self.num_job_in_batch = 0
        self.scheduled_rl = {}
        self.penalty = 0
        self.scheduled_f1 = {}

        job_sequence_size = JOB_SEQUENCE_SIZE
        
        # randomly sample a sequence of jobs from workload (self.start_idx_last_reset + 1) % (self.loads.size() - 2 * job_sequence_size)
        self.start = self.np_random.randint(job_sequence_size, (self.loads.size() - job_sequence_size - 1))
        # self.start = 1208
        self.start_idx_last_reset = self.start
        self.num_job_in_batch = job_sequence_size
        self.last_job_in_batch = self.start + self.num_job_in_batch
        self.current_timestamp = self.loads[self.start].submit_time
        self.job_queue.append(self.loads[self.start])
        self.next_arriving_job_idx = self.start + 1

        if self.enable_preworkloads:
            # Generate some running jobs to randomly fill the cluster.
            # @todo: let's change the running_job_size to a random number.
            q_workloads = []
            running_job_size = self.np_random.randint(2 * job_sequence_size)
            for i in range(running_job_size):
                _job = self.loads[self.start - i - 1]
                req_num_of_processors = _job.request_number_of_processors
                runtime_of_job = _job.run_time
                job_tmp = Job()
                job_tmp.job_id = (-1 - i)  # to be different from the normal jobs; normal jobs have a job_id >= 0
                job_tmp.request_number_of_processors = req_num_of_processors
                job_tmp.run_time = runtime_of_job
                if self.cluster.can_allocated(job_tmp):
                    self.running_jobs.append(job_tmp)
                    job_tmp.scheduled_time = max(0, (self.current_timestamp - random.randint(0, max(runtime_of_job, 1))))
                    # job_tmp.scheduled_time = max(0, (self.current_timestamp - runtime_of_job/2))
                    job_tmp.allocated_machines = self.cluster.allocate(job_tmp.job_id, job_tmp.request_number_of_processors)
                    q_workloads.append(job_tmp)
                else:
                    break

        # schedule the sequence of jobs using heuristic algorithm. 
        while True:
            self.job_queue.sort(key=lambda j: self.f1_score(j))
            job_for_scheduling = self.job_queue[0]

            # make sure we move forward and release needed resources
            if not self.cluster.can_allocated(job_for_scheduling):
                self.moveforward_for_resources(job_for_scheduling)
            
            assert job_for_scheduling.scheduled_time == -1  # this job should never be scheduled before.

            job_for_scheduling.scheduled_time = self.current_timestamp
            job_for_scheduling.allocated_machines = self.cluster.allocate(job_for_scheduling.job_id,
                                                                            job_for_scheduling.request_number_of_processors)
            self.running_jobs.append(job_for_scheduling)
            score = (self.job_score(job_for_scheduling) / self.num_job_in_batch)  # calculated reward
            self.scheduled_f1[job_for_scheduling.job_id] = score
            self.job_queue.remove(job_for_scheduling)

            not_empty = self.moveforward_for_job()

            if not not_empty:
                break

            '''
            get_this_job_scheduled = False
            self.job_queue.sort(key=lambda j: self.sjf_score(j))
            if self.cluster.can_allocated(self.job_queue[0]):
                job_for_scheduling = self.job_queue.pop(0)
                assert job_for_scheduling.scheduled_time == -1  # this job should never be scheduled before.
                job_for_scheduling.scheduled_time = self.current_timestamp
                job_for_scheduling.allocated_machines = self.cluster.allocate(job_for_scheduling.job_id,
                                                                            job_for_scheduling.request_number_of_processors)
                self.running_jobs.append(job_for_scheduling)
                _tmp = self.job_score(job_for_scheduling)
                self.scheduled_f1[job_for_scheduling.job_id] = (_tmp / self.num_job_in_batch)
                get_this_job_scheduled = True

            while not get_this_job_scheduled or not self.job_queue:
                if not self.running_jobs:  # there are no running jobs
                    next_resource_release_time = sys.maxsize  # always add jobs if no resource can be released.
                    next_resource_release_machines = []
                else:
                    self.running_jobs.sort(key=lambda running_job: (running_job.scheduled_time + running_job.run_time))
                    next_resource_release_time = (self.running_jobs[0].scheduled_time + self.running_jobs[0].run_time)
                    next_resource_release_machines = self.running_jobs[0].allocated_machines

                if self.next_arriving_job_idx < self.last_job_in_batch \
                        and self.loads[self.next_arriving_job_idx].submit_time <= next_resource_release_time:
                    self.job_queue.append(self.loads[self.next_arriving_job_idx])
                    self.current_timestamp = self.loads[self.next_arriving_job_idx].submit_time
                    self.next_arriving_job_idx += 1
                    break
                else:
                    if not self.running_jobs:
                        break
                    self.current_timestamp = next_resource_release_time
                    self.cluster.release(next_resource_release_machines)
                    self.running_jobs.pop(0)  # remove the first running job.

            done = True
            for i in range(self.start, self.last_job_in_batch):
                if self.loads[i].scheduled_time == -1:  # have at least one job in the batch who has not been scheduled
                    done = False
                    break
            if done:
                break
            '''

        # reset again
        self.cluster.reset()
        self.loads.reset()
        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []
        self.current_timestamp = self.loads[self.start].submit_time
        self.job_queue.append(self.loads[self.start])
        self.last_job_in_batch = self.start + self.num_job_in_batch
        self.next_arriving_job_idx = self.start + 1

        if self.enable_preworkloads:
            # use the same jobs to fill the cluster.
            for job_tmp in q_workloads:
                self.running_jobs.append(job_tmp)
                job_tmp.allocated_machines = self.cluster.allocate(job_tmp.job_id, job_tmp.request_number_of_processors)

        done = False
        # if there is only one job, try to schedule it and move forward. Really no need to let agent learn.
        while not done and self.has_only_one_job():
            # print ("in reset, schedule: ", self.job_queue[0])
            done = self.schedule(self.job_queue[0])

        if done:
            return self.reset()
        else:
            return self.build_observation()

    def build_observation(self):
        vector = np.zeros((MAX_QUEUE_SIZE) * JOB_FEATURES, dtype=float)
        self.job_queue.sort(key=lambda job: self.sjf_score(job))
        self.visible_jobs = []
        for i in range(0, MAX_QUEUE_SIZE):
            if i < len(self.job_queue):
                self.visible_jobs.append(self.job_queue[i])
            else:
                break
        self.visible_jobs.sort(key=lambda j: self.sjf_score(j))

        self.pairs = []
        for i in range(0, MAX_QUEUE_SIZE): # always keep the last job as "do nothing and wait"
            if i < len(self.visible_jobs):
                job = self.visible_jobs[i]
                submit_time = job.submit_time
                request_processors = job.request_number_of_processors
                request_time = job.request_time
                # run_time = job.run_time
                wait_time = self.current_timestamp - submit_time

                # make sure that larger value is better.
                normalized_wait_time = min(float(wait_time) / float(MAX_WAIT_TIME), 1.0 - 1e-8)
                normalized_run_time = min(float(request_time) / float(self.loads.max_exec_time), 1.0 - 1e-8)
                normalized_request_nodes = min(float(request_processors) / float(self.loads.max_procs), 1.0 - 1e-8)
                if self.cluster.can_allocated(job):
                    can_schedule_now = 1
                else:
                    can_schedule_now = 0
                self.pairs.append([job,normalized_wait_time, normalized_run_time, normalized_request_nodes, can_schedule_now])        
            else:
                self.pairs.append([None,0,1,1,0])

        # random.shuffle(self.pairs)   # agent sees jobs in random order

        for i in range(0, MAX_QUEUE_SIZE):
            vector[i*JOB_FEATURES:(i+1)*JOB_FEATURES] = self.pairs[i][1:]

        return vector

    def moveforward_for_resources(self, job):
        while not self.cluster.can_allocated(job):
            assert self.running_jobs
            self.running_jobs.sort(key=lambda running_job: (running_job.scheduled_time + running_job.run_time))
            next_resource_release_time = (self.running_jobs[0].scheduled_time + self.running_jobs[0].run_time)
            next_resource_release_machines = self.running_jobs[0].allocated_machines
                
            if self.next_arriving_job_idx < self.last_job_in_batch \
            and self.loads[self.next_arriving_job_idx].submit_time <= next_resource_release_time:
                assert self.current_timestamp <= self.loads[self.next_arriving_job_idx].submit_time
                self.current_timestamp = self.loads[self.next_arriving_job_idx].submit_time
                self.job_queue.append(self.loads[self.next_arriving_job_idx])
                self.next_arriving_job_idx += 1
            else:
                assert self.current_timestamp <= next_resource_release_time
                self.current_timestamp = next_resource_release_time
                self.cluster.release(next_resource_release_machines)
                self.running_jobs.pop(0)  # remove the first running job
    
    def moveforward_for_job(self):
        if self.job_queue:
            return True

        # if we need to add job, but can not add any more, return False indicating the job_queue is for sure empty now.
        if self.next_arriving_job_idx >= self.last_job_in_batch:
            return False

        # move forward to add jobs into job queue.
        while not self.job_queue:
            if not self.running_jobs:  # there are no running jobs
                next_resource_release_time = sys.maxsize  # always add jobs if no resource can be released.
                next_resource_release_machines = []
            else:
                self.running_jobs.sort(key=lambda running_job: (running_job.scheduled_time + running_job.run_time))
                next_resource_release_time = (self.running_jobs[0].scheduled_time + self.running_jobs[0].run_time)
                next_resource_release_machines = self.running_jobs[0].allocated_machines

            if self.loads[self.next_arriving_job_idx].submit_time <= next_resource_release_time:
                assert self.current_timestamp <= self.loads[self.next_arriving_job_idx].submit_time
                self.current_timestamp = self.loads[self.next_arriving_job_idx].submit_time
                self.job_queue.append(self.loads[self.next_arriving_job_idx])
                self.next_arriving_job_idx += 1
                return True     # job added
            else:
                assert self.current_timestamp <= next_resource_release_time
                self.current_timestamp = next_resource_release_time
                self.cluster.release(next_resource_release_machines)
                self.running_jobs.pop(0)  # remove the first running job.
            
    def create_penalty_job(self):
        # if the agent picks an empty job, we punish it as if it schedules a job which requests all nodes, lasts maximal time, 
        # another idea: if the agent picks an empty job, we consider it delays the scheduling. We can simply consider it is
        # a job requesting 0 processors, running for a small time amount?
        penalty_job = Job()
        penalty_job.request_number_of_processors = self.cluster.total_node * self.cluster.num_procs_per_node
        penalty_job.job_id = PENALTY_JOB_ID
        penalty_job.submit_time = self.current_timestamp
        penalty_job.run_time = self.loads.max_exec_time
        return penalty_job

    def job_score(self, job_for_scheduling):
        _tmp = max(1.0, (float(job_for_scheduling.scheduled_time - job_for_scheduling.submit_time + job_for_scheduling.run_time)
                        /
                        max(job_for_scheduling.run_time, 10)))
        # Weight larger jobs.
        #_tmp = _tmp * (job_for_scheduling.run_time * job_for_scheduling.request_number_of_processors)
        return _tmp

    def has_only_one_job(self):
        if len(self.job_queue) == 1:
            return True
        else:
            return False

    def schedule(self, job_for_scheduling):
        # create penalty job for illegal action
        if not job_for_scheduling:
            job_for_scheduling = self.create_penalty_job()  # create the penalty job
            # self.visible_jobs.sort(key=lambda j: self.sjf_score(j))
            # job_for_scheduling = self.visible_jobs[-1]

        # make sure we move forward and release needed resources
        if not self.cluster.can_allocated(job_for_scheduling):
            self.moveforward_for_resources(job_for_scheduling)

        # we should be OK to schedule the job now
        assert job_for_scheduling.scheduled_time == -1  # this job should never be scheduled before.
        job_for_scheduling.scheduled_time = self.current_timestamp
        job_for_scheduling.allocated_machines = self.cluster.allocate(job_for_scheduling.job_id, job_for_scheduling.request_number_of_processors)
        self.running_jobs.append(job_for_scheduling)

        # if the job is a penalty job, no need to move it out of job queue as it was never inserted
        if job_for_scheduling.job_id != PENALTY_JOB_ID:
            score = (self.job_score(job_for_scheduling) / self.num_job_in_batch)  # calculated reward
            #reward = (self.scheduled_f1[job_for_scheduling.job_id] - score)
            self.scheduled_rl[job_for_scheduling.job_id] = score
            self.job_queue.remove(job_for_scheduling)  # remove the job from job queue
        else:
            print ("Got Penalty Job")
            # reward = (1 - self.penalty_job_score)
            self.penalty += self.penalty_job_score

        # after scheduling, check if job queue is empty, try to add jobs. 
        not_empty = self.moveforward_for_job()

        if not_empty:
            # job_queue is not empty
            return False
        else:
            # job_queue is empty and can not add new jobs as we reach the end of the sequence
            # f1_total = sum(self.scheduled_f1.values())
            # rl_total = sum(self.scheduled_rl.values())
            return True

    def valid(self, a):
        action = a[0]
        return self.pairs[action][0]
        
    def step(self, a):
        job_for_scheduling = self.pairs[a][0]
        done = self.schedule(job_for_scheduling)

        # if there is only one job, schedule it and move forward
        while not done and self.has_only_one_job():
            done = self.schedule(self.job_queue[0])

        if not done:
            obs = self.build_observation()
            return [obs, 0, False, None]
        else:
            rl_total = sum(self.scheduled_rl.values())
            f1_total = sum(self.scheduled_f1.values())
            #print (self.scheduled_f1)
            #print ("------------------------")
            #print (self.scheduled_rl)
            return [None, (f1_total - rl_total), True, None]

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--workload', type=str, default='./data/lublin_256.swf')  # RICC-2010-2
    args = parser.parse_args()
    current_dir = os.getcwd()
    workload_file = os.path.join(current_dir, args.workload)

    env = HPCEnv()
    env.my_init(workload_file=workload_file, sched_file=workload_file)
    env.seed(0)

    for _ in range(100):
        _, r = env.reset(), 0
        while True:
            _, r, d, _ = env.step(0)
            if d:
                print ("Final Reward:", r)
                break