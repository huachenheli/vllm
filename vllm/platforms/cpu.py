# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import torch

from vllm.logger import init_logger
from vllm.utils import DEFAULT_MAX_NUM_BATCHED_TOKENS

from .interface import CpuArchEnum, Platform, PlatformEnum, _Backend

logger = init_logger(__name__)

if TYPE_CHECKING:
    from vllm.config import VllmConfig
else:
    VllmConfig = None


def get_max_threads(pid=0):
    if hasattr(os, 'sched_getaffinity'):
        return len(os.sched_getaffinity(pid))
    elif platform.system() == 'Darwin':
        return os.cpu_count()
    else:
        raise NotImplementedError("Unsupported OS")


@dataclass
class LogicalCPUInfo:
    id: int = -1
    physical_core: int = -1
    numa_node: int = -1

    @classmethod
    def _int(cls, value: str) -> int:
        try:
            int_value = int(value)
        except Exception:
            int_value = -1
        return int_value

    @staticmethod
    def json_decoder(obj_dict: dict):
        id = obj_dict.get("cpu")
        physical_core = obj_dict.get("core")
        numa_node = obj_dict.get("node")

        if not (id is None or physical_core is None or numa_node is None):
            return LogicalCPUInfo(
                id=LogicalCPUInfo._int(id),
                physical_core=LogicalCPUInfo._int(physical_core),
                numa_node=LogicalCPUInfo._int(numa_node))
        else:
            return obj_dict


class CpuPlatform(Platform):
    _enum = PlatformEnum.CPU
    device_name: str = "cpu"
    device_type: str = "cpu"
    dispatch_key: str = "CPU"
    dist_backend: str = "gloo"

    @property
    def supported_dtypes(self) -> list[torch.dtype]:
        if self.get_cpu_architecture() == CpuArchEnum.POWERPC:
            return [torch.bfloat16, torch.float32]
        elif sys.platform.startswith(
                "darwin") and self.get_cpu_architecture() == CpuArchEnum.ARM:
            # TODO: change this condition to check if the platform support bf16
            # instead of checking the OS. For instance M2 shall supports bf16
            # already. But we need to modify `cpu_extension.cmake` to activate
            # the feature in the build.
            return [torch.float16, torch.float32]
        # x86/aarch64 CPU has supported both bf16 and fp16 natively.
        return [torch.bfloat16, torch.float16, torch.float32]

    @classmethod
    def get_device_name(cls, device_id: int = 0) -> str:
        return "cpu"

    @classmethod
    def get_attn_backend_cls(cls, selected_backend: _Backend, head_size: int,
                             dtype: torch.dtype, kv_cache_dtype: Optional[str],
                             block_size: int, use_v1: bool,
                             use_mla: bool) -> str:
        if selected_backend and selected_backend != _Backend.TORCH_SDPA:
            logger.info("Cannot use %s backend on CPU.", selected_backend)
        if use_mla:
            raise NotImplementedError("MLA is not supported on CPU.")
        logger.info("Using Torch SDPA backend.")
        if not use_v1:
            raise ValueError("CPU backend only supports V1.")
        return "vllm.v1.attention.backends.cpu_attn.TorchSDPABackend"

    @classmethod
    def get_device_total_memory(cls, device_id: int = 0) -> int:
        import vllm.envs as envs
        from vllm.utils import GiB_bytes

        kv_cache_space = envs.VLLM_CPU_KVCACHE_SPACE
        if kv_cache_space is None:
            kv_cache_space = 4 * GiB_bytes  # type: ignore
            logger.warning_once(
                "Environment variable VLLM_CPU_KVCACHE_SPACE (GiB) "
                "for CPU backend is not set, using 4 by default.")
        else:
            kv_cache_space *= GiB_bytes

        return kv_cache_space

    @classmethod
    def set_device(cls, device: torch.device) -> None:
        """
        Set the device for the current platform.
        """
        torch.cpu.set_device(device)

    @classmethod
    def is_async_output_supported(cls, enforce_eager: Optional[bool]) -> bool:
        return False

    @classmethod
    def inference_mode(cls):
        return torch.no_grad()

    @classmethod
    def check_and_update_config(cls, vllm_config: VllmConfig) -> None:
        model_config = vllm_config.model_config

        if model_config is not None:
            model_config.disable_cascade_attn = True

        cache_config = vllm_config.cache_config

        ipex_available = find_spec("intel_extension_for_pytorch") is not None

        if cache_config and cache_config.block_size is None:
            cache_config.block_size = 128 if ipex_available else 16

        if not ipex_available and cache_config.block_size != 16:
            raise RuntimeError(
                f"--block-size={cache_config.block_size} requires"
                " intel_extension_for_pytorch")

        scheduler_config = vllm_config.scheduler_config
        if ((scheduler_config.chunked_prefill_enabled
             or cache_config.enable_prefix_caching)
                and cache_config.cache_dtype != "auto"):
            raise RuntimeError("Chunked-prefill and prefix-cache on the CPU "
                               "backend is not compatible with FP8 KV cache.")

        if cache_config.cache_dtype == "fp8_e4m3":
            cache_config.cache_dtype = "fp8_e5m2"
            logger.warning(
                "CPU backend doesn't support fp8_e4m3 KV cache type, "
                "cast to fp8_e5m2.")

        if (cache_config.cache_dtype != "auto" and model_config is not None
                and model_config.dtype == torch.half):
            logger.warning("FP8 KV cache on the CPU backend only does not"
                           " support fp16 for now, cast to bf16.")
            model_config.dtype = torch.bfloat16

        cache_config.cpu_kvcache_space_bytes = \
            CpuPlatform.get_device_total_memory()

        parallel_config = vllm_config.parallel_config
        if (parallel_config.world_size > 1
                and parallel_config.distributed_executor_backend is not None
                and parallel_config.distributed_executor_backend != "mp"):
            logger.warning(("%s is not supported on CPU, fallback to mp "
                            "distributed executor backend."),
                           parallel_config.distributed_executor_backend)
            parallel_config.distributed_executor_backend = "mp"
        if parallel_config.worker_cls == "auto":
            parallel_config.worker_cls = "vllm.v1.worker.cpu_worker.CPUWorker"

        # Note: workaround for v1 gpu_model_runner
        from vllm.config import CompilationLevel
        vllm_config.compilation_config.cudagraph_capture_sizes = []

        compilation_config = vllm_config.compilation_config
        if vllm_config.compilation_config.level == CompilationLevel.PIECEWISE:

            # Note: vLLM V1 is using PIECEWISE level compilation, which will
            # take time to compile kernels just-in-time with the inductor
            # backend. For CPU CI tests, most of them are executed fast and
            # compilations consume too much time, even with torch compile
            # cache. So use VLLM_CPU_CI_ENV to indicate the CI environment,
            # and just execute model with dynamo + eager mode to save time.
            # VLLM_CPU_CI_ENV is only used as an internal variable.
            if os.environ.get("VLLM_CPU_CI_ENV", "0") != "0":
                backend = "eager"
            else:
                backend = "inductor"

            compilation_config.level = CompilationLevel.DYNAMO_ONCE
            compilation_config.backend = backend
            compilation_config.inductor_compile_config.update({
                "dce":
                True,
                "size_asserts":
                False,
                "nan_asserts":
                False,
                "epilogue_fusion":
                True,
            })
            if compilation_config.use_inductor:
                compilation_config.custom_ops = ["none"]

        if vllm_config.lora_config is not None:
            compilation_config.level = CompilationLevel.NO_COMPILATION

        assert vllm_config.device_config.device_type == "cpu"

        #
        # Environment variables for CPU executor
        #

        os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"

        # Note: to avoid the error 'nthreads cannot be larger than environment
        #  variable "NUMEXPR_MAX_THREADS" (64)'.
        os.environ["NUMEXPR_MAX_THREADS"] = str(get_max_threads())

        # Set default threads num for OpenMP parallel
        os.environ["OMP_NUM_THREADS"] = str(torch.get_num_threads())

        # Disable torch async compiling which won't work with daemonic processes
        os.environ["TORCHINDUCTOR_COMPILE_THREADS"] = "1"

        # Intel OpenMP setting
        ld_prealod_str = os.getenv("LD_PRELOAD", "")
        if "libiomp5.so" in ld_prealod_str:
            # The time(milliseconds) that a thread should wait after
            # completing the execution of a parallel region, before sleeping.
            os.environ['KMP_BLOCKTIME'] = "1"
            # Prevents the CPU to run into low performance state
            os.environ['KMP_TPAUSE'] = "0"
            # Provides fine granularity parallelism
            os.environ['KMP_FORKJOIN_BARRIER_PATTERN'] = "dist,dist"
            os.environ['KMP_PLAIN_BARRIER_PATTERN'] = "dist,dist"
            os.environ['KMP_REDUCTION_BARRIER_PATTERN'] = "dist,dist"

        # To hint IPEX uses shared memory based AllReduce
        os.environ["LOCAL_WORLD_SIZE"] = str(
            vllm_config.parallel_config.tensor_parallel_size)

        if model_config is not None and model_config.use_mla:
            logger.info(
                "MLA is enabled on a non-GPU platform; forcing chunked "
                "prefill and prefix caching to be disabled.")
            vllm_config.scheduler_config.enable_chunked_prefill = False
            vllm_config.scheduler_config.chunked_prefill_enabled = False
            vllm_config.scheduler_config.max_num_batched_tokens = max(
                vllm_config.scheduler_config.max_model_len,
                DEFAULT_MAX_NUM_BATCHED_TOKENS)

    @classmethod
    def get_allowed_cpu_memory_node_list(
            cls) -> tuple[list[int], list[LogicalCPUInfo]]:
        assert platform.system() == "Linux"

        # Init LogicalCPUInfo from lscpu
        lscpu_output = subprocess.check_output("lscpu -J -e=CPU,CORE,NODE",
                                               shell=True,
                                               text=True)
        logical_cpu_list: list[LogicalCPUInfo] = json.loads(
            lscpu_output, object_hook=LogicalCPUInfo.json_decoder)['cpus']

        # Filter CPUs with invalid attributes
        logical_cpu_list = [
            x for x in logical_cpu_list
            if -1 not in (x.id, x.physical_core, x.numa_node)
        ]

        # Filter allowed CPUs
        allowed_cpu_id_list = os.sched_getaffinity(0)
        logical_cpu_list = [
            x for x in logical_cpu_list if x.id in allowed_cpu_id_list
        ]

        # Get allowed NUMA nodes
        allowed_numa_nodes = set()
        for x in logical_cpu_list:
            allowed_numa_nodes.add(x.numa_node)  # type: ignore
        allowed_numa_nodes_list = sorted(allowed_numa_nodes)

        return allowed_numa_nodes_list, logical_cpu_list

    @classmethod
    def is_pin_memory_available(cls) -> bool:
        logger.warning("Pin memory is not supported on CPU.")
        return False

    @classmethod
    def get_punica_wrapper(cls) -> str:
        return "vllm.lora.punica_wrapper.punica_cpu.PunicaWrapperCPU"

    @classmethod
    def get_device_communicator_cls(cls) -> str:
        """
        Get device specific communicator class for distributed communication.
        """
        return "vllm.distributed.device_communicators.cpu_communicator.CpuCommunicator"  # noqa

    @classmethod
    def supports_structured_output(cls) -> bool:
        return True

    @classmethod
    def supports_v1(cls, model_config) -> bool:
        """Returns whether the current platform can support v1 for the supplied
        model configuration.
        """
        return True

    @classmethod
    def default_v1(cls, model_config) -> bool:
        """Returns whether the current platform can use v1 by default for the
        supplied model configuration.
        """
        arch = cls.get_cpu_architecture()
        return (cls.supports_v1(model_config) and arch
                in (CpuArchEnum.X86, CpuArchEnum.POWERPC, CpuArchEnum.ARM))
