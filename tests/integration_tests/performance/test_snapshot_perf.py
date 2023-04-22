# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Basic tests scenarios for snapshot save/restore."""

import json
import os
import platform

import pytest

import host_tools.logging as log_tools
from framework.stats import consumer, producer, types
from framework.utils import CpuMap

# How many latencies do we sample per test.
SAMPLE_COUNT = 3
USEC_IN_MSEC = 1000
PLATFORM = platform.machine()

# measurement without pass criteria = test is infallible but still submits metrics. Nice!
LATENCY_MEASUREMENT = types.MeasurementDef.create_measurement(
    "latency",
    "ms",
    [],
    {},
)


def snapshot_create_producer(vm, target_version, metrics_fifo):
    """Produce results for snapshot create tests."""
    vm.snapshot_full(target_version=target_version)
    metrics = vm.flush_metrics(metrics_fifo)

    value = metrics["latencies_us"]["full_create_snapshot"] / USEC_IN_MSEC

    print(f"Latency {value} ms")

    return value


def snapshot_resume_producer(microvm_factory, snapshot, metrics_fifo):
    """Produce results for snapshot resume tests."""

    microvm = microvm_factory.build()
    microvm.spawn()
    microvm.restore_from_snapshot(snapshot, resume=True)

    # Attempt to connect to resumed microvm.
    # Verify if guest can run commands.
    exit_code, _, _ = microvm.ssh.execute_command("ls")
    assert exit_code == 0

    value = 0
    # Parse all metric data points in search of load_snapshot time.
    metrics = microvm.get_all_metrics(metrics_fifo)
    for data_point in metrics:
        metrics = json.loads(data_point)
        cur_value = metrics["latencies_us"]["load_snapshot"] / USEC_IN_MSEC
        if cur_value > 0:
            value = cur_value
            break

    print("Latency {value} ms")
    return value


def test_older_snapshot_resume_latency(
    microvm_factory,
    guest_kernel,
    rootfs,
    firecracker_release,
    io_engine,
    st_core,
):
    """
    Test scenario: Older snapshot load performance measurement.

    With each previous firecracker version, create a snapshot and try to
    restore in current version.
    """

    # The guest kernel does not "participate" in snapshot restore, so just pick
    # some arbitrary one
    if "4.14" not in guest_kernel.name:
        pytest.skip("just test one guest kernel")

    vcpus, guest_mem_mib = 2, 512
    microvm_cfg = f"{vcpus}vcpu_{guest_mem_mib}mb.json"
    vm = microvm_factory.build(
        guest_kernel,
        rootfs,
        monitor_memory=False,
        fc_binary_path=firecracker_release.path,
        jailer_binary_path=firecracker_release.jailer,
    )
    metrics_fifo_path = os.path.join(vm.path, "metrics_fifo")
    metrics_fifo = log_tools.Fifo(metrics_fifo_path)
    vm.spawn(metrics_path=metrics_fifo_path)
    vm.basic_config(vcpu_count=vcpus, mem_size_mib=guest_mem_mib)
    vm.add_net_iface()
    vm.start()
    # Check if guest works.
    exit_code, _, _ = vm.ssh.execute_command("ls")
    assert exit_code == 0
    snapshot = vm.snapshot_full()

    st_core.name = "older_snapshot_resume_latency"
    st_core.iterations = SAMPLE_COUNT
    st_core.custom["guest_config"] = microvm_cfg.strip(".json")
    st_core.custom["io_engine"] = io_engine
    st_core.custom["snapshot_type"] = "FULL"

    prod = producer.LambdaProducer(
        func=snapshot_resume_producer,
        func_kwargs={
            "microvm_factory": microvm_factory,
            "snapshot": snapshot,
            "metrics_fifo": metrics_fifo,
        },
    )

    cons = consumer.LambdaConsumer(
        func=lambda cons, result: cons.consume_stat(
            st_name="max", ms_name="latency", value=result
        ),
        func_kwargs={},
    )
    cons.set_measurement_def(LATENCY_MEASUREMENT)

    st_core.add_pipe(producer=prod, consumer=cons, tag=microvm_cfg)
    # Gather results and verify pass criteria.
    st_core.run_exercise()


def test_snapshot_create_latency(
    microvm_factory,
    guest_kernel,
    rootfs,
    firecracker_release,
    st_core,
):
    """Measure the latency of creating a Full snapshot"""

    # The guest kernel does not "participate" in snapshot restore, so just pick
    # some arbitrary one
    if "4.14" not in guest_kernel.name:
        pytest.skip("just test one guest kernel")

    guest_mem_mib = 512
    vcpus = 2
    microvm_cfg = f"{vcpus}vcpu_{guest_mem_mib}mb.json"
    vm = microvm_factory.build(guest_kernel, rootfs, monitor_memory=False)
    metrics_fifo_path = os.path.join(vm.path, "metrics_fifo")
    metrics_fifo = log_tools.Fifo(metrics_fifo_path)
    vm.spawn(metrics_path=metrics_fifo_path)
    vm.basic_config(
        vcpu_count=vcpus,
        mem_size_mib=guest_mem_mib,
    )
    vm.start()

    # Check if the needed CPU cores are available. We have the API
    # thread, VMM thread and then one thread for each configured vCPU.
    assert CpuMap.len() >= 2 + vm.vcpus_count

    # Pin uVM threads to physical cores.
    current_cpu_id = 0
    assert vm.pin_vmm(current_cpu_id), "Failed to pin firecracker thread."
    current_cpu_id += 1
    assert vm.pin_api(current_cpu_id), "Failed to pin fc_api thread."
    for idx_vcpu in range(vm.vcpus_count):
        current_cpu_id += 1
        assert vm.pin_vcpu(
            idx_vcpu, current_cpu_id + idx_vcpu
        ), f"Failed to pin fc_vcpu {idx_vcpu} thread."

    st_core.name = "snapshot_create_SnapshotType.FULL_latency"
    st_core.iterations = SAMPLE_COUNT
    st_core.custom["guest_config"] = microvm_cfg.strip(".json")
    st_core.custom["snapshot_type"] = "FULL"

    prod = producer.LambdaProducer(
        func=snapshot_create_producer,
        func_kwargs={
            "vm": vm,
            "target_version": firecracker_release.snapshot_version,
            "metrics_fifo": metrics_fifo,
        },
    )

    cons = consumer.LambdaConsumer(
        func=lambda cons, result: cons.consume_stat(
            st_name="max", ms_name="latency", value=result
        ),
        func_kwargs={},
    )
    cons.set_measurement_def(LATENCY_MEASUREMENT)

    st_core.add_pipe(producer=prod, consumer=cons, tag=microvm_cfg)
    # Gather results and verify pass criteria.
    st_core.run_exercise()
