# Copyright 2020 Uber Technologies, Inc. All Rights Reserved.
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
# ==============================================================================

from __future__ import absolute_import

import logging
import os
import threading
import time

from collections import defaultdict

from six.moves import queue

from horovod.run.common.util import hosts, timeout
from horovod.run.elastic.discovery import DiscoveredHosts
from horovod.run.elastic.worker import WorkerNotificationClient


READY = 'READY'
SUCCESS = 'SUCCESS'
FAILURE = 'FAILURE'

DISCOVER_HOSTS_FREQUENCY_SECS = 1.0
START_TIMEOUT_SECS = 600


def _epoch_time_s():
    return int(time.time())


class WorkerStateRegistry(object):
    def __init__(self, driver, verbose=False):
        self._driver = driver
        self._lock = threading.Lock()
        self._states = {}
        self._workers = defaultdict(set)
        self._barrier = None
        self._rendezvous_id = 0
        self._verbose = verbose
        self._size = 0

    def get(self, state):
        return self._workers[state]

    def count(self, state):
        return len(self._workers[state])

    def reset(self, size):
        with self._lock:
            logging.info('reset workers: {}'.format(size))
            self._states.clear()
            self._workers.clear()
            self._barrier = threading.Barrier(parties=size, action=self._action)
            self._rendezvous_id += 1
            self._size = size

    def size(self):
        return self._size

    def last_rendezvous(self):
        return self._rendezvous_id

    def record_ready(self, host, slot):
        return self._record_state(host, slot, READY)

    def record_success(self, host, slot):
        return self._record_state(host, slot, SUCCESS)

    def record_failure(self, host, slot):
        return self._record_state(host, slot, FAILURE)

    def _record_state(self, host, slot, state):
        if self._driver.finished():
            logging.info('driver finished, ignoring registration: {}[{}] = {}'.format(host, slot, state))
            return self._rendezvous_id

        key = (host, slot)
        with self._lock:
            if key in self._states:
                logging.info('key exists, reset barrier: {}[{}] = {}'.format(host, slot, state))
                self._barrier.reset()
            logging.info('record state: {}[{}] = {}'.format(host, slot, state))
            self._states[key] = state
            self._workers[state].add(key)
            rendezvous_id = self._rendezvous_id

        rendezvous_id = self._wait(key, state, rendezvous_id)
        return rendezvous_id

    def _wait(self, key, state, rendezvous_id):
        while True:
            try:
                self._barrier.wait()
                return rendezvous_id
            except threading.BrokenBarrierError:
                if self._barrier.broken:
                    # Timeout or other non-recoverable error, so exit
                    raise

                with self._lock:
                    rendezvous_id = self._rendezvous_id
                    saved_state = self._states.get(key, state)
                    if saved_state != state:
                        raise RuntimeError('State {} overridden by {}'.format(state, saved_state))

    def _action(self):
        self._driver.on_workers_recorded()


class Results(object):
    def __init__(self):
        self._results = {}
        self._worker_threads = queue.Queue()

    def expect(self, worker_thread):
        self._worker_threads.put(worker_thread)

    def add_result(self, key, value):
        if key in self._results:
            return
        self._results[key] = value

    def get_results(self):
        while not self._worker_threads.empty():
            worker_thread = self._worker_threads.get()
            worker_thread.join()
        return self._results


class ElasticDriver(object):
    def __init__(self, rendezvous, discovery, min_np, max_np, start_timeout=None, verbose=0):
        self._rendezvous = rendezvous
        self._discovered_hosts = DiscoveredHosts(discovery)
        self._min_np = min_np
        self._max_np = max_np
        self._verbose = verbose

        self._assigned_hosts = []
        self._host_assignments = {}
        self._world_size = 0

        self._wait_hosts_cond = threading.Condition()
        self._start_timeout = start_timeout or int(os.getenv('HOROVOD_ELASTIC_START_TIMEOUT', START_TIMEOUT_SECS))

        self._create_worker_fn = None
        self._worker_clients = {}

        self._worker_registry = WorkerStateRegistry(self)
        self._results = Results()
        self._shutdown = threading.Event()

        self._discovery_thread = threading.Thread(target=self._discover_hosts)
        self._discovery_thread.daemon = True
        self._discovery_thread.start()

    def start(self, np, create_worker_fn):
        self._create_worker_fn = create_worker_fn
        self._activate_hosts(np)

    def wait_for_available_hosts(self, min_np, max_np=None):
        tmout = timeout.Timeout(
            self._start_timeout,
            message='Timed out waiting for {{activity}}. Please check that you have '
                    'enough resources to run at least {min_np} Horovod processes.'.format(min_np=min_np))

        self._wait_hosts_cond.acquire()
        try:
            while not self._has_available_slots(self._discovered_hosts.count_available_slots(), min_np, max_np):
                self._wait_hosts_cond.wait(tmout.remaining())
                tmout.check_time_out_for('minimum number of hosts to become available')
        finally:
            self._wait_hosts_cond.release()

    def _has_available_slots(self, slots, min_np, max_np):
        return slots >= min_np and (max_np is None or slots <= max_np)

    def get_results(self):
        return self._results.get_results()

    def stop(self):
        self._shutdown.set()
        self._discovery_thread.join()

    def finished(self):
        return self._shutdown.is_set()

    def register_worker_server(self, host, slot, addresses, secret_key):
        self._worker_clients[(host, slot)] = WorkerNotificationClient(
            addresses, secret_key, self._verbose)

    def record_ready(self, host, slot):
        self._worker_registry.record_ready(host, slot)

    def on_workers_recorded(self):
        logging.info('all {} workers recorded'.format(self._worker_registry.size()))

        # Check for success state, if any process succeeded, shutdown all other processes
        if self._worker_registry.count(SUCCESS) > 0:
            logging.info('success count == {} -> stop running'.format(self._worker_registry.count(SUCCESS)))
            self.stop()
            return

        # Check that all processes failed, indicating that processing should stop
        if self._worker_registry.count(FAILURE) == self._world_size:
            logging.error('failure count == {} -> stop running'.format(self._world_size))
            self.stop()
            return

        # Check for failures, and add them to the blacklisted hosts list
        failures = self._worker_registry.get(FAILURE)
        for host, slot in failures:
            self._discovered_hosts.blacklist(host)

        # If there are no active hosts that aren't blacklisted, treat this as job failure
        blacklisted_slots = self._discovered_hosts.count_blacklisted_slots()
        if blacklisted_slots == self._world_size:
            logging.error('blacklisted slots count == {} -> stop running'.format(self._world_size))
            self.stop()
            return

        try:
            self._activate_hosts(self._min_np)
        except Exception:
            logging.exception('failed to activate new hosts -> stop running')
            self.stop()

    def world_size(self):
        return self._world_size

    def local_size(self, host):
        return len(self._host_assignments[host])

    def get_slot_info(self, host, slot):
        return self._host_assignments[host][slot] if self.has_rank_assignment(host, slot) \
            else hosts.INVALID_SLOT_INFO

    def has_rank_assignment(self, host, slot):
        if self._discovered_hosts.is_blacklisted(host):
            return False
        return host in self._host_assignments and len(self._host_assignments[host]) > slot

    def get_available_hosts(self):
        return self._discovered_hosts.get_available_hosts()

    def _activate_hosts(self, min_np):
        logging.info('wait for available hosts: {}'.format(min_np))
        self.wait_for_available_hosts(min_np)
        new_assigned_hosts = self._update_assigned_hosts()
        self._worker_registry.reset(self.world_size())
        for host in new_assigned_hosts:
            logging.info('start worker processes: {}'.format(host))
            self._start_worker_processes(host)

    def _discover_hosts(self):
        while not self._shutdown.is_set():
            self._wait_hosts_cond.acquire()
            try:
                if self._discovered_hosts.update_available_hosts():
                    self._notify_workers_host_changes()
                    self._wait_hosts_cond.notify_all()
            finally:
                self._wait_hosts_cond.release()
            self._shutdown.wait(DISCOVER_HOSTS_FREQUENCY_SECS)

    def _notify_workers_host_changes(self):
        timestamp = _epoch_time_s()
        for (host, slot), client in self._worker_clients.items():
            try:
                client.notify_hosts_updated(timestamp)
            except:
                if self._verbose >= 2:
                    print('WARNING: failed to notify {}[{}] of host updates'
                          .format(host, slot))

    def _update_assigned_hosts(self):
        new_assigned_hosts = []
        self._assigned_hosts = self._discovered_hosts.filter_available_hosts(self._assigned_hosts)
        current_hosts = set(self._assigned_hosts)
        for host in self.get_available_hosts():
            if host not in current_hosts:
                new_assigned_hosts.append(host)
                self._assigned_hosts.append(host)
        self._update_host_assignments()
        return new_assigned_hosts

    def _update_host_assignments(self):
        host_list = [hosts.HostInfo(host, self._discovered_hosts.get_slots(host)) for host in self._assigned_hosts]
        host_assignments_list = hosts.get_host_assignments(host_list, self._min_np, self._max_np)
        host_assignments = defaultdict(list)
        for slot_info in host_assignments_list:
            host_assignments[slot_info.hostname].append(slot_info)
        self._host_assignments = host_assignments
        self._world_size = len(host_assignments_list)
        self._rendezvous.httpd.init(host_assignments_list)

    def _start_worker_processes(self, host):
        for slot_info in self._host_assignments[host]:
            self._start_worker_process(slot_info)

    def _start_worker_process(self, slot_info):
        create_worker_fn = self._create_worker_fn
        shutdown_event = self._shutdown
        host_event = self._discovered_hosts.get_host_event(slot_info.hostname)

        def run_worker():
            res = create_worker_fn(slot_info, [shutdown_event, host_event])
            exit_code, timestamp = res
            self._handle_worker_exit(slot_info, exit_code, timestamp)

        thread = threading.Thread(target=run_worker)
        thread.daemon = True
        thread.start()
        self._results.expect(thread)

    def _handle_worker_exit(self, slot_info, exit_code, timestamp):
        if not self.has_rank_assignment(slot_info.hostname, slot_info.local_rank):
            # Ignore hosts that are not assigned a rank
            logging.debug('host {} has been blacklisted, ignoring exit from local_rank={}'
                          .format(slot_info.hostname, slot_info.local_rank))
            return

        if exit_code == 0:
            rendezvous_id = self._worker_registry.record_success(slot_info.hostname, slot_info.local_rank)
        else:
            rendezvous_id = self._worker_registry.record_failure(slot_info.hostname, slot_info.local_rank)

        if self.finished() and self._worker_registry.last_rendezvous() == rendezvous_id:
            logging.debug('adding results for {}[{}]: ({}, {})'
                          .format(slot_info.hostname, slot_info.local_rank, exit_code, timestamp))
            name = '{}[{}]'.format(slot_info.hostname, slot_info.local_rank)
            self._results.add_result(name, (exit_code, timestamp))

