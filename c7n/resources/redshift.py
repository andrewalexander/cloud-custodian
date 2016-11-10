# Copyright 2016 Capital One Services, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import functools
import json
import logging

from botocore.exceptions import ClientError
from concurrent.futures import as_completed

from c7n.actions import ActionRegistry, BaseAction, ModifyGroupsAction
from c7n.filters import (
    FilterRegistry, ValueFilter, DefaultVpcBase, AgeFilter, OPERATORS)
import c7n.filters.vpc as net_filters

from c7n.manager import resources
from c7n.query import QueryResourceManager
from c7n import tags
from c7n.utils import (
    type_schema, local_session, chunks, generate_arn, get_retry,
    get_account_id, snapshot_identifier)

log = logging.getLogger('custodian.redshift')

filters = FilterRegistry('redshift.filters')
actions = ActionRegistry('redshift.actions')
filters.register('marked-for-op', tags.TagActionFilter)


@resources.register('redshift')
class Redshift(QueryResourceManager):

    resource_type = "aws.redshift.cluster"
    filter_registry = filters
    action_registry = actions
    retry = staticmethod(get_retry(('Throttling',)))

    _generate_arn = _account_id = None

    @property
    def account_id(self):
        if self._account_id is None:
            session = local_session(self.session_factory)
            self._account_id = get_account_id(session)
        return self._account_id

    @property
    def generate_arn(self):
        if self._generate_arn is None:
            self._generate_arn = functools.partial(
                generate_arn, 'redshift', region=self.config.region,
                account_id=self.account_id, resource_type='cluster',
                separator=':')
        return self._generate_arn


@filters.register('default-vpc')
class DefaultVpc(DefaultVpcBase):
    """ Matches if an redshift database is in the default vpc
    """

    schema = type_schema('default-vpc')

    def __call__(self, redshift):
        return (redshift.get('VpcId') and
                self.match(redshift.get('VpcId')) or False)


@filters.register('security-group')
class SecurityGroupFilter(net_filters.SecurityGroupFilter):

    RelatedIdsExpression = "VpcSecurityGroups[].VpcSecurityGroupId"


@filters.register('subnet')
class SubnetFilter(net_filters.SubnetFilter):

    RelatedIdsExpression = ""

    def get_related_ids(self, resources):
        group_ids = set()
        for r in resources:
            group_ids.update(
                [s['SubnetIdentifier'] for s in
                 self.groups[r['ClusterSubnetGroupName']]['Subnets']])
        return group_ids

    def process(self, resources, event=None):
        self.groups = {r['ClusterSubnetGroupName']: r for r in
                       RedshiftSubnetGroup(self.manager.ctx, {}).resources()}
        return super(SubnetFilter, self).process(resources, event)


@filters.register('param')
class Parameter(ValueFilter):

    schema = type_schema('param', rinherit=ValueFilter.schema)
    group_params = ()

    def process(self, clusters, event=None):
        groups = {}
        for r in clusters:
            for pg in r['ClusterParameterGroups']:
                groups.setdefault(pg['ParameterGroupName'], []).append(
                    r['ClusterIdentifier'])

        def get_params(group_name):
            c = local_session(self.manager.session_factory).client('redshift')
            param_group = c.describe_cluster_parameters(
                ParameterGroupName=group_name)['Parameters']
            params = {}
            for p in param_group:
                v = p['ParameterValue']
                if v != 'default' and p['DataType'] in ('integer', 'boolean'):
                    # overkill..
                    v = json.loads(v)
                params[p['ParameterName']] = v
            return params

        with self.executor_factory(max_workers=3) as w:
            group_names = groups.keys()
            self.group_params = dict(
                zip(group_names, w.map(get_params, group_names)))
        return super(Parameter, self).process(clusters, event)

    def __call__(self, db):
        params = {}
        for pg in db['ClusterParameterGroups']:
            params.update(self.group_params[pg['ParameterGroupName']])
        return self.match(params)


@actions.register('delete')
class Delete(BaseAction):

    schema = type_schema(
        'delete', **{'skip-snapshot': {'type': 'boolean'}})

    def process(self, clusters):
        with self.executor_factory(max_workers=2) as w:
            futures = []
            for db_set in chunks(clusters, size=5):
                futures.append(
                    w.submit(self.process_db_set, db_set))
            for f in as_completed(futures):
                if f.exception():
                    self.log.error(
                        "Exception deleting redshift set \n %s",
                        f.exception())

    def process_db_set(self, db_set):
        skip = self.data.get('skip-snapshot', False)
        c = local_session(self.manager.session_factory).client('redshift')
        for db in db_set:
            params = {'ClusterIdentifier': db['ClusterIdentifier']}
            if skip:
                params['SkipFinalClusterSnapshot'] = True
            else:
                params['FinalClusterSnapshotIdentifier'] = snapshot_identifier(
                    'Final', db['ClusterIdentifier'])
            try:
                c.delete_cluster(**params)
            except ClientError as e:
                if e.response['Error']['Code'] == "InvalidClusterState":
                    self.log.warning(
                        "Cannot delete cluster when not 'Available' state: %s",
                        db['ClusterIdentifier'])
                    continue
                raise


@actions.register('retention')
class RetentionWindow(BaseAction):

    date_attribute = 'AutomatedSnapshotRetentionPeriod'
    schema = type_schema(
        'retention',
        **{'days': {'type': 'number'}})

    def process(self, clusters):
        with self.executor_factory(max_workers=2) as w:
            futures = []
            for cluster in clusters:
                futures.append(w.submit(
                    self.process_snapshot_retention,
                    cluster))
            for f in as_completed(futures):
                if f.exception():
                    self.log.error(
                        "Exception setting Redshift retention  \n %s",
                        f.exception())

    def process_snapshot_retention(self, cluster):
        current_retention = int(cluster.get(self.date_attribute, 0))
        new_retention = self.data['days']

        if current_retention < new_retention:
            self.set_retention_window(
                cluster,
                max(current_retention, new_retention))
            return cluster

    def set_retention_window(self, cluster, retention):
        c = local_session(self.manager.session_factory).client('redshift')
        c.modify_cluster(
            ClusterIdentifier=cluster['ClusterIdentifier'],
            AutomatedSnapshotRetentionPeriod=retention)


@actions.register('snapshot')
class Snapshot(BaseAction):

    schema = type_schema('snapshot')

    def process(self, clusters):
        with self.executor_factory(max_workers=3) as w:
            futures = []
            for cluster in clusters:
                futures.append(w.submit(
                    self.process_cluster_snapshot,
                    cluster))
            for f in as_completed(futures):
                if f.exception():
                    self.log.error(
                        "Exception creating Redshift snapshot  \n %s",
                        f.exception())
        return clusters

    def process_cluster_snapshot(self, cluster):
        c = local_session(self.manager.session_factory).client('redshift')
        c.create_cluster_snapshot(
            SnapshotIdentifier=snapshot_identifier(
                'Backup',
                cluster['ClusterIdentifier']),
            ClusterIdentifier=cluster['ClusterIdentifier'])


@actions.register('mark-for-op')
class TagDelayedAction(tags.TagDelayedAction):

    schema = type_schema('mark-for-op', rinherit=tags.TagDelayedAction.schema)

    def process_resource_set(self, resources, tags):
        client = local_session(self.manager.session_factory).client('redshift')
        for r in resources:
            arn = self.manager.generate_arn(r['ClusterIdentifier'])
            client.create_tags(ResourceName=arn, Tags=tags)


@actions.register('tag')
class Tag(tags.Tag):

    concurrency = 2
    batch_size = 5

    def process_resource_set(self, resources, tags):
        client = local_session(self.manager.session_factory).client('redshift')
        for r in resources:
            arn = self.manager.generate_arn(r['ClusterIdentifer'])
            client.create_tags(ResourceName=arn, Tags=tags)


@actions.register('unmark')
@actions.register('remove-tag')
class RemoveTag(tags.RemoveTag):

    concurrency = 2
    batch_size = 5

    def process_resource_set(self, resources, tag_keys):
        client = local_session(self.manager.session_factory).client('redshift')
        for r in resources:
            arn = self.manager.generate_arn(r['ClusterIdentifier'])
            client.delete_tags(ResourceName=arn, TagKeys=tag_keys)


@actions.register('tag-trim')
class TagTrim(tags.TagTrim):

    max_tag_count = 10

    def process_tag_removal(self, resource, candidates):
        client = local_session(self.manager.session_factory).client('redshift')
        arn = self.manager.generate_arn(resource['DBInstanceIdentifier'])
        client.delete_tags(ResourceName=arn, TagKeys=candidates)


@resources.register('redshift-subnet-group')
class RedshiftSubnetGroup(QueryResourceManager):
    """Redshift subnet group."""

    class resource_type(object):
        service = 'redshift'
        type = 'redshift-subnet-group'
        id = name = 'ClusterSubnetGroupName'
        enum_spec = (
            'describe_cluster_subnet_groups', 'ClusterSubnetGroups', None)
        filter_name = 'ClusterSubnetGroupName'
        filter_type = 'scalar'
        dimension = None
        date = None


@resources.register('redshift-snapshot')
class RedshiftSnapshot(QueryResourceManager):
    """Resource manager for Redshift snapshots.
    """

    filter_registry = FilterRegistry('redshift-snapshot.filters')
    action_registry = ActionRegistry('redshift-snapshot.actions')

    filter_registry.register('marked-for-op', tags.TagActionFilter)

    _generate_arn = _account_id = None

    @property
    def account_id(self):
        if self._account_id is None:
            session = local_session(self.session_factory)
            self._account_id = get_account_id(session)
        return self._account_id

    @property
    def generate_arn(self):
        if self._generate_arn is None:
            self._generate_arn = functools.partial(
                generate_arn, 'redshift', region=self.config.region,
                account_id=self.account_id, resource_type='snapshot',
                separator=':')
        return self._generate_arn

    class resource_type(object):

        service = 'redshift'
        type = 'redshift-snapshot'
        enum_spec = ('describe_cluster_snapshots', 'Snapshots', None)
        name = id = 'SnapshotIdentifier'
        filter_name = None
        filter_type = None
        dimension = None
        date = 'SnapshotCreateTime'


@actions.register('modify-groups')
class RedshiftModifyGroups(ModifyGroupsAction):
    """Modify security groups on an Redshift cluster"""

    schema = type_schema(
        'modify-groups',
        **{'groups': {'anyOf': [
            {'type': 'string', 'enum': ['matched', 'all']},
            {'type': 'array', 'items': {'type': 'string'}}]},
           'isolation-group': {'type': 'string'}})

    def process(self, clusters):
        client = local_session(self.manager.session_factory).client('redshift')
        groups = super(RedshiftModifyGroups, self).get_groups(resources)
        for idx, c in enumerate(clusters):
            client.modify_cache_cluster(
                ClusterIdentifier=c['ClusterIdentifier'],
                VpcSecurityGroupIds=groups[idx])


@RedshiftSnapshot.filter_registry.register('age')
class RedshiftSnapshotAge(AgeFilter):

    schema = type_schema(
        'age', days={'type': 'number'},
        op={'type': 'string', 'enum': OPERATORS.keys()})

    date_attribute = 'SnapshotCreateTime'


@RedshiftSnapshot.action_registry.register('delete')
class RedshiftSnapshotDelete(BaseAction):

    def process(self, snapshots):
        log.info("Deleting %d Redshift snapshots", len(snapshots))
        with self.executor_factory(max_workers=3) as w:
            futures = []
            for snapshot_set in chunks(reversed(snapshots), size=50):
                futures.append(
                    w.submit(self.process_snapshot_set, snapshot_set))
            for f in as_completed(futures):
                if f.exception():
                    self.log.error(
                        "Exception deleting snapshot set \n %s",
                        f.exception())
        return snapshots

    def process_snapshot_set(self, snapshots_set):
        c = local_session(self.manager.session_factory).client('redshift')
        for s in snapshots_set:
            c.delete_cluster_snapshot(
                SnapshotIdentifier=s['SnapshotIdentifier'],
                SnapshotClusterIdentifier=s['ClusterIdentifier'])


@RedshiftSnapshot.action_registry.register('mark-for-op')
class RedshiftSnapshotTagDelayedAction(tags.TagDelayedAction):

    schema = type_schema('mark-for-op', rinherit=tags.TagDelayedAction.schema)

    def process_resource_set(self, resources, tags):
        client = local_session(self.manager.session_factory).client('redshift')
        for r in resources:
            arn = self.manager.generate_arn(
                r['ClusterIdentifier'] + '/' + r['SnapshotIdentifier'])
            client.create_tags(ResourceName=arn, Tags=tags)


@RedshiftSnapshot.action_registry.register('tag')
class RedshiftSnapshotTag(tags.Tag):

    concurrency = 2
    batch_size = 5

    def process_resource_set(self, resources, tags):
        client = local_session(self.manager.session_factory).client('redshift')
        for r in resources:
            arn = self.manager.generate_arn(r['SnapshotIdentifer'])
            client.create_tags(ResourceName=arn, Tags=tags)


@RedshiftSnapshot.action_registry.register('unmark')
@RedshiftSnapshot.action_registry.register('remove-tag')
class RedshiftSnapshotRemoveTag(tags.RemoveTag):

    concurrency = 2
    batch_size = 5

    def process_resource_set(self, resources, tag_keys):
        client = local_session(self.manager.session_factory).client('redshift')
        for r in resources:
            arn = self.manager.generate_arn(r['SnapshotIdentifier'])
            client.delete_tags(ResourceName=arn, TagKeys=tag_keys)
