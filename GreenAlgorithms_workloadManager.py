
## ~~~ TO BE EDITED TO BE TAILORED TO THE WORKLOAD MANAGER ~~~
##
## This script is designed for SLURM
##

import subprocess
import pandas as pd
from io import BytesIO
import datetime
import os

class Helpers_WM():

    def convert_to_GB(self, memory, unit):
        '''
        Convert data quantity into GB.
        :param memory: [float] quantity to convert
        :param unit: [str] unit of `memory`, has to be in ['M', 'G', 'K']
        :return: [float] memory in GB.
        '''
        assert unit in ['M', 'G', 'K']
        if unit == 'M':
            memory /= 1e3
        elif unit == 'K':
            memory /= 1e6
        return memory

    def calc_ReqMem(self, x):
        '''
        Calculate the total memory required when submitting the job.
        :param x: [pd.Series] one row of sacct output.
        :return: [float] total required memory, in GB.
        '''
        mem_raw, n_nodes, n_cores = x['ReqMem'], x['NNodes'], x['NCPUS']

        unit = mem_raw[-2]
        per_coreOrNode = mem_raw[-1]
        memory = float(mem_raw[:-2])

        # Convert memory to GB
        memory = self.convert_to_GB(memory,unit)

        # Multiply by number of nodes/cores
        assert per_coreOrNode in ['n','c']
        if per_coreOrNode == 'c':
            memory *= n_cores
        else:
            memory *= n_nodes

        return memory

    def clean_RSS(self, x, cluster_info):
        '''
        Clean the RSS value in sacct output.
        :param x: [NaN or str] the RSS value, either NaN or of the form '2745K'
        (optionally, just a number, we then use default_unit_RSS from cluster_info.yaml as unit).
        :return: [float] RSS value, in GB.
        '''
        if pd.isnull(x)|(x=='0'):
            memory = 0
        else:
            assert type(x) == str
            # Special case for the situation where MaxRSS is of the form '154264' without a unit.
            if x[-1].isalpha():
                memory = self.convert_to_GB(float(x[:-1]),x[-1])
            else:
                assert 'default_unit_RSS' in cluster_info, "Some values of MaxRSS don't have a unit. Please specify a default_unit_RSS in cluster_info.yaml"
                memory = self.convert_to_GB(float(x), cluster_info['default_unit_RSS'])

        return memory

    def clean_partition(self, x, cluster_info):
        '''
        Clean the partition field, by replacing NaNs with empty string
        and selecting just one partition per job.
        :param x: [str] partition or comma-seperated list of partitions
        :param cluster_info: [dict]
        :return: [str] one partition or empty string
        '''
        if pd.isnull(x):
            return ''
        else:
            L_partitions = x.split(',')
            L_TDP = [cluster_info['partitions'][p]['TDP'] for p in L_partitions]
            assert len(set(L_TDP)) == 1, f'Different cores for the different partitions specified for a same job: {x}'
            assert all([p in cluster_info['partitions'] for p in L_partitions]), f"Unrecognised partition: {x}"
            # TODO: perhaps use the average in this case?
            return L_partitions[0]

    def parse_timedelta(self, x):
        '''
        Parse a string representing a duration into a `datetime.timedelta` object.
        :param x: [str] Duration, as '[DD-HH:MM:]SS[.MS]'
        :return: [datetime.timedelta] Timedelta object
        '''
        # Parse number of days
        day_split = x.split('-')
        if len(day_split) == 2:
            n_days = int(day_split[0])
            HHMMSSms = day_split[1]
        else:
            n_days = 0
            HHMMSSms = x

        # Parse ms
        ms_split = HHMMSSms.split('.')
        if len(ms_split) == 2:
            n_ms = int(ms_split[1])
            HHMMSS = ms_split[0]
        else:
            n_ms = 0
            HHMMSS = HHMMSSms

        # Parse HH,MM,SS
        last_split = HHMMSS.split(':')
        if len(last_split) == 3:
            to_add = []
        elif len(last_split) == 2:
            to_add = ['00']
        elif len(last_split) == 1:
            to_add = ['00','00']
        n_h, n_m, n_s = list(map(int, to_add + last_split))

        timeD = datetime.timedelta(
            days=n_days,
            hours=n_h,
            minutes=n_m,
            seconds=n_s,
            milliseconds=n_ms
        )
        return timeD

    def calc_realMemNeeded(self, x, granularity_memory_request):
        '''
        Calculate the minimum memory needed.
        This is calculated as the smallest multiple of `granularity_memory_request` that is greater than maxRSS.
        :param x: [pd.Series] one row of sacct output.
        :param  granularity_memory_request: [float or int] level of granularity available when requesting memory on this cluster
        :return: [float] minimum memory needed, in GB.
        '''
        return min(x.ReqMemX,(int(x.UsedMemX/granularity_memory_request)+1)*granularity_memory_request)

    def clean_State(self, x):
        '''
        Standardise the job's state, coding with {-1,0,1}
        :param x: [str] "State" field from sacct output
        :return: [int] in [-1,0,1]
        '''
        if x in ['CD','COMPLETED']:
            return 1
        elif x in ['PD','PENDING','R','RUNNING','RQ','REQUEUED']:
            return -1
        else:
            return 0

    def get_parent_jobID(self, x):
        '''
        Get the parent job ID in case of array jobs
        :param x: [str] JobID of the form 123456789_0 (with or without '_0')
        :return: [str] Parent ID 123456789
        '''
        foo = x.split('_')
        assert len(foo) <= 2, f"Can't parse the job ID: {x}"
        return foo[0]


class WorkloadManager(Helpers_WM):

    def __init__(self, args, cluster_info):
        '''
        Methods related to the Workload manager
        :param args: [Namespace] input from the user
        :param cluster_info: [dict] information about this specific cluster.
        '''
        self.args = args
        self.cluster_info = cluster_info
        super().__init__()

    def pull_logs(self):
        '''
        Run the command line to pull usage from the workload manager.
        '''
        bash_com = [
            "sacct",
            "--starttime",
            self.args.startDay,  # format YYYY-MM-DD
            "--endtime",
            self.args.endDay,  # format YYYY-MM-DD
            "--format",
            "JobID,JobName,Submit,Elapsed,Partition,NNodes,NCPUS,TotalCPU,ReqMem,MaxRSS,WorkDir,State",
            "-P"
        ]

        if self.args.useLoggedOutput == '':
            logs = subprocess.run(bash_com, capture_output=True)
            self.logs_raw = logs.stdout
        else:
            print(f"Overrriding logs_raw with: {self.args.useLoggedOutput}")
            with open(os.path.join('testData', self.args.useLoggedOutput), 'rb') as f:
                self.logs_raw = f.read()

    def convert2dataframe(self):
        '''
        Convert raw logs output into a pandas dataframe.
        '''
        logs_df = pd.read_csv(BytesIO(self.logs_raw), sep="|", dtype='str')
        for x in ['NNodes', 'NCPUS']:
            logs_df[x] = logs_df[x].astype('int64')

        self.logs_df = logs_df

    def clean_logs_df(self):
        '''
        Clean the different fields of the usage logs.
        NB: the name of the columns here (ending with X) need to be conserved, as they are used by the main script.
        '''
        ### Calculate real memory usage
        self.logs_df['ReqMemX'] = self.logs_df.apply(self.calc_ReqMem, axis=1)

        ### Clean MaxRSS
        self.logs_df['UsedMemX'] = self.logs_df.MaxRSS.apply(self.clean_RSS, cluster_info=self.cluster_info)

        ### Parse wallclock time
        self.logs_df['WallclockTimeX'] = self.logs_df['Elapsed'].apply(self.parse_timedelta)

        ### Parse total CPU time
        self.logs_df['TotalCPUtimeX'] = self.logs_df['TotalCPU'].apply(self.parse_timedelta)

        ### Clean partition
        # Make sure it's either a partition name, or a comma-separated list of partitions
        self.logs_df['PartitionX'] = self.logs_df.Partition.apply(
            self.clean_partition,
            cluster_info=self.cluster_info
        )

        ### Parse submit datetime
        self.logs_df['SubmitDatetimeX'] = self.logs_df.Submit.apply(lambda x: datetime.datetime.strptime(x, "%Y-%m-%dT%H:%M:%S"))

        ### Number of CPUs
        # e.g. here there is no cleaning necessary, so I just standardise the column name
        self.logs_df['NCPUSX'] = self.logs_df.NCPUS

        ### Number of nodes
        self.logs_df['NNodesX'] = self.logs_df.NNodes

        ### Job name
        self.logs_df['JobNameX'] = self.logs_df.JobName

        ### Working directory
        self.logs_df['WorkingDirX'] = self.logs_df.WorkDir

        ### State
        self.logs_df['StateX'] = self.logs_df.State.apply(self.clean_State)

        ### Pull jobID
        self.logs_df['single_jobID'] = self.logs_df.JobID.apply(lambda x: x.split('.')[0])

        ### Aggregate per jobID
        self.df_agg_0 = self.logs_df.groupby('single_jobID').agg({
            'TotalCPUtimeX': 'max',
            'WallclockTimeX': 'max',
            'ReqMemX': 'max',
            'UsedMemX': 'max',
            'NCPUSX': 'max',
            'NNodesX': 'max',
            'PartitionX': lambda x: ''.join(x),
            'JobNameX': 'first',
            'SubmitDatetimeX': 'min',
            'WorkingDirX': 'first',
            'StateX': 'min',
        })

        ### Remove jobs that are still running or currently queued
        self.df_agg = self.df_agg_0.loc[self.df_agg_0.StateX != -1]

        ### Calculate real memory need
        self.df_agg['NeededMemX'] = self.df_agg.apply(
            self.calc_realMemNeeded,
            granularity_memory_request=self.cluster_info['granularity_memory_request'],
            axis=1)

        ### Add memory waste information
        # TODO can be overestimated
        self.df_agg['memOverallocationFactorX'] = (self.df_agg.ReqMemX - self.df_agg.NeededMemX) / self.df_agg.NeededMemX

        ### Filter on working directory
        if self.args.filterWD is not None:
            self.df_agg = self.df_agg.loc[self.df_agg.WorkingDirX == self.args.filterWD]

        ### Filter on Job ID
        self.df_agg.reset_index(inplace=True)
        self.df_agg['parentJobID'] = self.df_agg.single_jobID.apply(self.get_parent_jobID)

        if self.args.filterJobIDs != 'all':
            list_jobs2keep = self.args.filterJobIDs.split(',')
            self.df_agg = self.df_agg.loc[self.df_agg.parentJobID.isin(list_jobs2keep)]
