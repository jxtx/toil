# Copyright (C) 2015-2016 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
from abc import ABCMeta, abstractmethod

from collections import namedtuple

from itertools import islice

from toil.batchSystems.abstractBatchSystem import AbstractScalableBatchSystem
from toil.provisioners.aws.awsProvisioner import AWSProvisioner
from toil.provisioners.cgcloud.provisioner import CGCloudProvisioner

log = logging.getLogger(__name__)


Shape = namedtuple("_Shape", "wallTime memory cores disk")
"""
Represents a job or a node's "shape", in terms of the dimensions of memory, cores, disk and
wall-time allocation. All attributes are integers.

The wallTime attribute stores the number of seconds of a node allocation, e.g. 3600 for AWS,
or 60 for Azure. FIXME: and for jobs?

The memory and disk attributes store the number of bytes required by a job (or provided by a
node) in RAM or on disk (SSD or HDD), respectively.
"""


class Cluster(object):
    def __init__(self, clusterName, provisioner):
        self.clusterName = clusterName
        self.provisioner = None
        if provisioner == 'aws':
            self.provisioner = AWSProvisioner
        elif provisioner == 'cgcloud':
            self.provisioner = CGCloudProvisioner
        else:
            raise RuntimeError('The only options are aws and cgcloud')

    def sshCluster(self):
        self.provisioner.sshLeader(self.clusterName)

    def destroyCluster(self):
        self.provisioner.destroyCluster(self.clusterName)



class AbstractProvisioner(object):
    """
    An abstract base class to represent the interface for provisioning worker nodes to use in a
    Toil cluster.
    """

    __metaclass__ = ABCMeta

    def __init__(self, config, batchSystem):
        self.config = config
        self.batchSystem = batchSystem

    def setNodeCount(self, numNodes, preemptable=False, force=False):
        """
        Attempt to grow or shrink the number of prepemptable or non-preemptable worker nodes in
        the cluster to the given value, or as close a value as possible, and, after performing
        the necessary additions or removals of worker nodes, return the resulting number of
        preemptable or non-preemptable nodes currently in the cluster.

        :param int numNodes: Number of nodes to add.

        :param bool preemptable: whether the added nodes will be preemptable, i.e. whether they
               may be removed spontaneously by the underlying platform at any time.

        :param bool force: If False, the provisioner is allowed to deviate from the given number
               of nodes. For example, when downsizing a cluster, a provisioner might leave nodes
               running if they have active jobs running on them.

        :rtype: int :return: the number of nodes in the cluster after making the necessary
                adjustments. This value should be, but is not guaranteed to be, close or equal to
                the `numNodes` argument. It represents the closest possible approximation of the
                actual cluster size at the time this method returns.
        """
        workerInstances = self._getWorkersInCluster(preemptable)
        numCurrentNodes = len(workerInstances)
        delta = numNodes - numCurrentNodes
        if delta > 0:
            log.info('Adding %i nodes to get to desired cluster size of %i.', delta, numNodes)
            numNodes = numCurrentNodes + self._addNodes(workerInstances,
                                                        numNodes=delta,
                                                        preemptable=preemptable)
        elif delta < 0:
            log.info('Removing %i nodes to get to desired cluster size of %i.', -delta, numNodes)
            numNodes = numCurrentNodes - self._removeNodes(workerInstances,
                                                           numNodes=-delta,
                                                           preemptable=preemptable,
                                                           force=force)
        else:
            log.info('Cluster already at desired size of %i. Nothing to do.', numNodes)
        return numNodes

    def _removeNodes(self, instances, numNodes, preemptable=False, force=False):
        # If the batch system is scalable, we can use the number of currently running workers on
        # each node as the primary criterion to select which nodes to terminate.
        if isinstance(self.batchSystem, AbstractScalableBatchSystem):
            nodes = self.batchSystem.getNodes(preemptable)
            # Join nodes and instances on private IP address.
            nodes = [(instance, nodes.get(instance.private_ip_address)) for instance in instances]
            # Unless forced, exclude nodes with runnning workers. Note that it is possible for
            # the batch system to report stale nodes for which the corresponding instance was
            # terminated already. There can also be instances that the batch system doesn't have
            # nodes for yet. We'll ignore those, too, unless forced.
            nodes = [(instance, nodeInfo)
                     for instance, nodeInfo in nodes
                     if force or nodeInfo is not None and nodeInfo.workers < 1]
            # Sort nodes by number of workers and time left in billing cycle
            nodes.sort(key=lambda (instance, nodeInfo): (
                nodeInfo.workers if nodeInfo else 1,
                self._remainingBillingInterval(instance)))
            nodes = nodes[:numNodes]
            if log.isEnabledFor(logging.DEBUG):
                for instance, nodeInfo in nodes:
                    log.debug("Instance %s is about to be terminated. Its node info is %r. It "
                              "would be billed again in %s minutes.", instance.id, nodeInfo,
                              60 * self._remainingBillingInterval(instance))
            instanceIds = [instance.id for instance, nodeInfo in nodes]
        else:
            # Without load info all we can do is sort instances by time left in billing cycle.
            instances = sorted(instances,
                               key=lambda instance: (self._remainingBillingInterval(instance)))
            instanceIds = [instance.id for instance in islice(instances, numNodes)]
        log.info('Terminating %i instance(s).', len(instanceIds))
        if instanceIds:
            self._logAndTerminate(instanceIds)
        return len(instanceIds)

    @abstractmethod
    def _addNodes(self, instances, numNodes, preemptable):
        raise NotImplementedError

    @abstractmethod
    def _logAndTerminate(self, instanceIDs):
        raise NotImplementedError

    @abstractmethod
    def _getWorkersInCluster(self, preemptable):
        raise NotImplementedError

    @abstractmethod
    def _remainingBillingInterval(self, instance):
        raise NotImplementedError

    @abstractmethod
    def getNodeShape(self, preemptable=False):
        """
        The shape of a preemptable or non-preemptable node managed by this provisioner. The node
        shape defines key properties of a machine, such as its number of cores or the time
        between billing intervals.

        :param preemptable: Whether to return the shape of preemptable nodes or that of
               non-preemptable ones.

        :rtype: Shape
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def launchCluster(cls, instanceType, keyName, clusterName, spotBid=None):
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def sshLeader(cls, clusterName):
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def destroyCluster(cls, clusterName):
        raise NotImplementedError
