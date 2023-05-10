import boto3
import logging
from datetime import datetime, timedelta
from Constants import DEPLOY_ENV, AWS, SNAPSHOT_DAYS, MAX_SNAPSHOT_DAYS


class Snapshot:
    def __init__(self) -> None:
        self.tag = f'{DEPLOY_ENV}_staking_snapshot'
        self.ec2 = boto3.client('ec2')
        self.ssm = boto3.client('ssm')
        if AWS:
            self.volume_id = self.get_prefix_id('VOLUME')
            self.instance_id = self.get_prefix_id('INSTANCE')

    def is_older_than(self, snapshot, num_days):
        created = self.get_snapshot_time(snapshot)
        now = datetime.utcnow()
        actual_delta = now - created
        max_delta = timedelta(days=num_days)
        return actual_delta > max_delta

    def create(self, curr_snapshots):
        all_snapshots_are_old = all(
            [self.is_older_than(snapshot, SNAPSHOT_DAYS)
             for snapshot in curr_snapshots]
        )
        if all_snapshots_are_old:
            # Don't need to wait for 'completed' status
            # As soon as function returns,
            # old state is preserved while snapshot is in progress
            snapshot = self.ec2.create_snapshot(
                VolumeId=self.volume_id,
                TagSpecifications=[
                    {
                        'ResourceType': 'snapshot',
                        'Tags': [{'Key': 'type', 'Value': self.tag}]
                    }
                ]
            )
            self.ssm.put_parameter(
                Name=self.tag,
                Value=snapshot['SnapshotId'],
                Type='String',
                Overwrite=True,
                Tier='Standard',
                DataType='text'
            )

            return snapshot

    def get_prefix_id(self, prefix):
        with open(f'/mnt/ebs/{prefix}_ID', 'r') as file:
            id = file.read().strip()
        return id

    def get_snapshots(self):
        snapshots = self.ec2.describe_snapshots(
            Filters=[
                {
                    'Name': 'tag:type',
                    'Values': [self.tag]
                },
            ],
            OwnerIds=['self'],
        )['Snapshots']

        return snapshots

    def get_exceptions(self):
        exceptions = set()
        try:
            # Add existing snapshot id from ssm
            exceptions.add(self.ssm.get_parameter(
                Name=self.tag)['Parameter']['Value'])
        except Exception as e:
            logging.exception(e)

        try:
            # Add snapshot id from current instance's launch template
            if AWS:
                launch_template = self.ec2.get_launch_template_data(
                    InstanceId=self.instance_id)
                for device in launch_template['LaunchTemplateData']['BlockDeviceMappings']:
                    if device['DeviceName'] == '/dev/sdx':
                        exceptions.add(device['Ebs']['SnapshotId'])
                        break
        except Exception as e:
            logging.exception(e)

        return exceptions

    def get_snapshot_time(self, snapshot):
        return snapshot['StartTime'].replace(tzinfo=None)

    def find_most_recent(self, curr_snapshots):
        if not curr_snapshots:
            return None
        most_recent_idx = 0
        self.get_snapshot_time(curr_snapshots[0])
        for idx, snapshot in enumerate(curr_snapshots):
            if self.get_snapshot_time(snapshot) > self.get_snapshot_time(curr_snapshots[most_recent_idx]):
                most_recent_idx = idx

        return curr_snapshots[most_recent_idx]

    def purge(self, curr_snapshots, exceptions):

        purgeable = [
            snapshot for snapshot in curr_snapshots if self.is_older_than(
                snapshot, MAX_SNAPSHOT_DAYS
            ) and snapshot['SnapshotId'] not in exceptions
        ]

        for snapshot in purgeable:
            self.ec2.delete_snapshot(
                SnapshotId=snapshot['SnapshotId'],
            )

    def backup(self):
        curr_snapshots = self.get_snapshots()
        exceptions = self.get_exceptions()
        snapshot = self.create(curr_snapshots)
        self.purge(curr_snapshots, exceptions)
        return snapshot or self.find_most_recent(curr_snapshots)