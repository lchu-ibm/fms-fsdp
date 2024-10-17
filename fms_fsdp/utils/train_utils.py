import os
from functools import partial


try:
    import packaging.version
except ImportError:
    from pkg_resources import packaging  # type: ignore

import time
from datetime import timedelta

import torch.cuda.nccl as nccl
import torch.distributed as dist
from torch.distributed.fsdp import ShardingStrategy

from fms_fsdp.policies import *


def train(
    cfg,
    model,
    local_rank,
    rank,
    train_loader,
    optimizer,
    scheduler,
    # profiler,
    # checkpointer,
    start_step,
    tokens_seen,
):
    model.train()
    ddp_stats = torch.zeros(3).to(local_rank)

    if rank==0:
        print("    Training begins!")

    start = time.time()
    loop_start = time.time()
    train_loss = -1
    for batch_idx, (input, label) in enumerate(train_loader, start=start_step + 1):
        if batch_idx > cfg.num_steps:
            break
        input = input.to(local_rank)
        label = label.to(local_rank)

        optimizer.zero_grad()
        output = model(input)
        output = output.logits if hasattr(output, "logits") else output
        ce_loss = torch.nn.CrossEntropyLoss()
        loss = ce_loss(output.view(-1, output.size(-1)), label.view(-1).long())

        loss.backward()
        ddp_stats[1] += model.clip_grad_norm_(cfg.grad_clip_thresh).item()
        optimizer.step()
        scheduler.step()

        ddp_stats[0] += loss.item()
        ddp_stats[2] += 1

        # if profiler:
        #     profiler.step()

        if batch_idx % cfg.report_interval == 0:
            dist.all_reduce(ddp_stats, op=dist.ReduceOp.SUM)
            train_loss = ddp_stats[0] / ddp_stats[2]
            g_norm = ddp_stats[1] / ddp_stats[2]
            elapsed_time = time.time() - loop_start
            world_size = int(os.environ["WORLD_SIZE"])
            new_tokens_seen = (
                (batch_idx - start_step) * world_size * cfg.batch_size * cfg.seq_length
            )
            # if rank == 0:
            total_tokens_seen = tokens_seen + new_tokens_seen
            current_loss = train_loss.item()
            # current_lr = scheduler.get_last_lr()[0]
            current_gnorm = g_norm.item()
            current_step_time = (time.time() - start) / cfg.report_interval
            # overall_step_time = elapsed_time / (batch_idx - start_step)
            current_throughput = int(
                cfg.batch_size * cfg.seq_length / current_step_time
            )
            # overall_throughput = int(
            #     cfg.batch_size * cfg.seq_length / overall_step_time
            # )
            # reserved_mem = torch.cuda.max_memory_reserved(
            #     device=torch.cuda.current_device()
            # )
            # allocated_mem = torch.cuda.max_memory_allocated(
            #     device=torch.cuda.current_device()
            # )

            start = time.time()
            ddp_stats.zero_()
            if batch_idx > 999 and (torch.isnan(train_loss) or train_loss > 6):
                break
            
    if rank==0:
        print("    step:", min(batch_idx, cfg.num_steps))
        print("    loss:", current_loss)
        # print("    LR:", current_lr)
        print("    tokens seen:", total_tokens_seen)
        print("    gradient norm:", current_gnorm)
        # print("    reserved memory:", reserved_mem)
        # print("    allocated memory:", allocated_mem)
        print("    current step time:", current_step_time)
        # print("    overall step time:", overall_step_time)
        print("    current token per gpu per sec:", current_throughput)
        # print("    overall token per gpu per sec:", overall_throughput)
        # print(
        #     "    overall token per day:",
        #     int(new_tokens_seen / elapsed_time * 3600 * 24),
        # )
    return train_loss.item() if not torch.isnan(train_loss) else 100


def setup():
    dist.init_process_group("nccl", timeout=timedelta(seconds=60 * 60))


def setup_environ_flags():
    os.environ["TORCH_SHOW_CPP_STACKTRACES"] = str(1)
    os.environ["TORCH_NCCL_ASYNC_ERROR_HANDLING"] = str(1)
    # os.environ["NCCL_DEBUG"] = "INFO"


def get_mixed_precision_policy(cfg, rank):
    verify_bfloat_support = (
        torch.version.cuda
        and torch.cuda.is_bf16_supported()
        and packaging.version.parse(torch.version.cuda).release >= (11, 0)
        and dist.is_nccl_available()
        and nccl.version() >= (2, 10)
    )

    if cfg.mixed_precision:
        bf16_ready = verify_bfloat_support
        if bf16_ready:
            mixed_precision_policy = bfSixteen
            # if rank == 0:
            #     print(f"bFloat16 enabled for mixed precision - using bfSixteen policy")
        else:
            mixed_precision_policy = fpSixteen
            # if rank == 0:
            #     print(f"FP16 enabled")
    else:
        mixed_precision_policy = None

    return mixed_precision_policy


def get_policies(cfg, rank, block, model_cfg):
    """Get policies for mixed precision, wrapping, sharding, ac and param init function."""

    # mixed precision
    mixed_precision_policy = get_mixed_precision_policy(cfg, rank)

    # wrapping policy
    wrapping_policy = get_wrapper(block)

    # sharding strategy
    if cfg.sharding_strategy == "fsdp":
        sharding_strategy = ShardingStrategy.FULL_SHARD
    elif cfg.sharding_strategy == "hsdp":
        sharding_strategy = ShardingStrategy.HYBRID_SHARD
    elif cfg.sharding_strategy == "ddp":
        sharding_strategy = ShardingStrategy.NO_SHARD
    else:
        sharding_strategy = ShardingStrategy.FULL_SHARD
    # if rank == 0:
    #     print(f"Sharding strategy = {cfg.sharding_strategy}")

    # ac handler
    apply_selective_ac = partial(apply_fsdp_checkpointing, block=block)

    # param init function
    if cfg.low_cpu_fsdp:
        param_init_fn = partial(param_init_function, cfg=model_cfg)
    else:
        param_init_fn = None

    return (
        mixed_precision_policy,
        wrapping_policy,
        sharding_strategy,
        apply_selective_ac,
        param_init_fn,
    )


def get_profiler(cfg, rank):
    if not cfg.use_profiler:
        return
    if cfg.profiler_rank0_only and rank != 0:
        return
    return torch.profiler.profile(
        activities=[
            torch.profiler.ProfilerActivity.CPU,
            torch.profiler.ProfilerActivity.CUDA,
        ],
        schedule=torch.profiler.schedule(wait=1, warmup=2, active=3, repeat=1),
        on_trace_ready=torch.profiler.tensorboard_trace_handler("profile_traces"),
        profile_memory=True,
        with_stack=False,
        record_shapes=True,
    )
