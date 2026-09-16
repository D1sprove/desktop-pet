"""资源占用验收：启动主程序并采样 CPU / 内存。

用法：python tools/bench.py [秒数]   （默认 40 秒）
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    env = {"QT_QPA_PLATFORM": "offscreen"}
    import os
    e = dict(os.environ)
    e.update(env)
    proc = subprocess.Popen([PY, "main.py", "--smoke", str(secs)], cwd=ROOT, env=e)
    p = psutil.Process(proc.pid)
    cpu_samples, mem_samples = [], []
    t0 = time.time()
    print(f"采样 {secs:.0f}s ...")
    while time.time() - t0 < secs - 2:
        time.sleep(1.0)
        if proc.poll() is not None:
            break
        try:
            cpu_samples.append(p.cpu_percent(interval=None))
            mem_samples.append(p.memory_info().rss / 1024 / 1024)
        except Exception:
            break
    proc.wait()
    if cpu_samples:
        avg_cpu = sum(cpu_samples) / len(cpu_samples)
        # cpu_percent 是多核归一化，按核心数换算成"单核占比"
        per_core = avg_cpu / psutil.cpu_count()
        print(f"CPU 平均 : {avg_cpu:.2f}% (归一化) / 单核口径 {per_core:.2f}%")
        print(f"内存 RSS : 平均 {sum(mem_samples)/len(mem_samples):.1f} MB, "
              f"峰值 {max(mem_samples):.1f} MB")
        print(f"核心数   : {psutil.cpu_count()}")
        ok_cpu = per_core < 5.0
        ok_mem = max(mem_samples) < 200
        print(f"验收: CPU<5% {'PASS' if ok_cpu else 'FAIL'} | "
              f"内存<200MB {'PASS' if ok_mem else 'FAIL'}")
    else:
        print("未采到样本")


if __name__ == "__main__":
    main()
