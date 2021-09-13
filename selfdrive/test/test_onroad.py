#!/usr/bin/env python3
import json
import os
import subprocess
import time
import numpy as np
import unittest
from collections import Counter
from pathlib import Path

import cereal.messaging as messaging
from cereal.services import service_list
from common.basedir import BASEDIR
from common.timeout import Timeout
from common.params import Params
from selfdrive.hardware import EON, TICI
from selfdrive.loggerd.config import ROOT
from selfdrive.test.helpers import set_params_enabled
from tools.lib.logreader import LogReader

# Baseline CPU usage by process
PROCS = {
  "selfdrive.controls.controlsd": 50.0,
  "./loggerd": 45.0,
  "./locationd": 9.1,
  "selfdrive.controls.plannerd": 20.0,
  "./_ui": 15.0,
  "selfdrive.locationd.paramsd": 9.1,
  "./camerad": 7.07,
  "./_sensord": 6.17,
  "selfdrive.controls.radard": 5.67,
  "./_modeld": 4.48,
  "./boardd": 3.63,
  "./_dmonitoringmodeld": 2.67,
  "selfdrive.thermald.thermald": 2.41,
  "selfdrive.locationd.calibrationd": 2.0,
  "./_soundd": 2.0,
  "selfdrive.monitoring.dmonitoringd": 1.90,
  "./proclogd": 1.54,
  "selfdrive.logmessaged": 0.2,
  "./clocksd": 0.02,
  "./ubloxd": 0.02,
  "selfdrive.tombstoned": 0,
  "./logcatd": 0,
}

if EON:
  PROCS.update({
    "selfdrive.hardware.eon.androidd": 0.4,
  })

if TICI:
  PROCS.update({
    "./loggerd": 60.0,
    "selfdrive.controls.controlsd": 28.0,
    "./camerad": 31.0,
    "./_ui": 21.0,
    "selfdrive.controls.plannerd": 12.0,
    "selfdrive.locationd.paramsd": 5.0,
    "./_dmonitoringmodeld": 10.0,
    "selfdrive.thermald.thermald": 1.5,
  })


TIMINGS = {
  # rtols: max/min, rsd
  "can": [0.7, 0.1],
  "pandaState": [0.7, 0.1],
  "sendcan": [0.7, 0.1],
  "carState": [0.7, 0.1],
  "carControl": [0.7, 0.1],
  "controlsState": [0.7, 0.1],
  "lateralPlan": [0.7, 0.1],
  "roadCameraState": [0.6, 0.1],
  "driverCameraState": [0.6, 0.1],
  "modelV2": [0.7, 0.1],
  "driverState": [0.6, 0.1],
  "liveLocationKalman": [0.6, 0.1],
}
if EON:
  TIMINGS.update({
    "lateralPlan": [0.7, 0.2],
  })
if TICI:
  TIMINGS.update({
    "wideRoadCameraState": [0.6, 0.1],
  })


def cputime_total(ct):
  return ct.cpuUser + ct.cpuSystem + ct.cpuChildrenUser + ct.cpuChildrenSystem


def check_cpu_usage(first_proc, last_proc):
  result =  "------------------------------------------------\n"
  result += "------------------ CPU Usage -------------------\n"
  result += "------------------------------------------------\n"

  r = True
  dt = (last_proc.logMonoTime - first_proc.logMonoTime) / 1e9
  for proc_name, normal_cpu_usage in PROCS.items():
    first, last = None, None
    try:
      first = [p for p in first_proc.procLog.procs if proc_name in p.cmdline][0]
      last = [p for p in last_proc.procLog.procs if proc_name in p.cmdline][0]
      cpu_time = cputime_total(last) - cputime_total(first)
      cpu_usage = cpu_time / dt * 100.
      if cpu_usage > max(normal_cpu_usage * 1.15, normal_cpu_usage + 5.0):
        # cpu usage is high while playing sounds
        if proc_name == "./_soundd" and cpu_usage < 25.:
          continue
        result += f"Warning {proc_name} using more CPU than normal\n"
        r = False
      elif cpu_usage < min(normal_cpu_usage * 0.65, max(normal_cpu_usage - 1.0, 0.0)):
        result += f"Warning {proc_name} using less CPU than normal\n"
        r = False
      result += f"{proc_name.ljust(35)}  {cpu_usage:.2f}%\n"
    except IndexError:
      result += f"{proc_name.ljust(35)}  NO METRICS FOUND {first=} {last=}\n"
      r = False
  result += "------------------------------------------------\n"
  print(result)
  return r


class TestOnroad(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    if "DEBUG" in os.environ:
      segs = filter(lambda x: os.path.exists(os.path.join(x, "rlog.bz2")), Path(ROOT).iterdir())
      segs = sorted(segs, key=lambda x: x.stat().st_mtime)
      cls.lr = list(LogReader(os.path.join(segs[-2], "rlog.bz2")))
      return

    os.environ['SKIP_FW_QUERY'] = "1"
    os.environ['FINGERPRINT'] = "TOYOTA COROLLA TSS2 2019"
    set_params_enabled()

    # Make sure athena isn't running
    Params().delete("DongleId")
    Params().delete("AthenadPid")
    os.system("pkill -9 -f athena")

    logger_root = Path(ROOT)
    initial_segments = set()
    if logger_root.exists():
      initial_segments = set(Path(ROOT).iterdir())

    # start manager and run openpilot for a minute
    try:
      manager_path = os.path.join(BASEDIR, "selfdrive/manager/manager.py")
      proc = subprocess.Popen(["python", manager_path])

      sm = messaging.SubMaster(['carState'])
      with Timeout(150, "controls didn't start"):
        while sm.rcv_frame['carState'] < 0:
          sm.update(1000)

      # make sure we get at least two full segments
      cls.segments = []
      with Timeout(300, "timed out waiting for logs"):
        while len(cls.segments) < 3:
          new_paths = set()
          if logger_root.exists():
            new_paths = set(logger_root.iterdir()) - initial_segments
          segs = [p for p in new_paths if "--" in str(p)]
          cls.segments = sorted(segs, key=lambda s: int(str(s).rsplit('--')[-1]))
          time.sleep(5)

    finally:
      proc.terminate()
      if proc.wait(60) is None:
        proc.kill()

    cls.lr = list(LogReader(os.path.join(str(cls.segments[1]), "rlog.bz2")))

  def test_cloudlog_size(self):
    msgs = [m for m in self.lr if m.which() == 'logMessage']

    total_size = sum(len(m.as_builder().to_bytes()) for m in msgs)
    self.assertLess(total_size, 3.5e5)

    cnt = Counter([json.loads(m.logMessage)['filename'] for m in msgs])
    big_logs = [f for f, n in cnt.most_common(3) if n / sum(cnt.values()) > 30.]
    self.assertEqual(len(big_logs), 0, f"Log spam: {big_logs}")

  def test_cpu_usage(self):
    proclogs = [m for m in self.lr if m.which() == 'procLog']
    self.assertGreater(len(proclogs), service_list['procLog'].frequency * 45, "insufficient samples")
    cpu_ok = check_cpu_usage(proclogs[0], proclogs[-1])
    self.assertTrue(cpu_ok)

  def test_model_execution_timings(self):
    # TODO: this went up when plannerd cpu usage increased, why?
    cfgs = [("modelV2", 0.038, 0.036), ("driverState", 0.028, 0.026)]
    for (s, instant_max, avg_max) in cfgs:
      ts = [getattr(getattr(m, s), "modelExecutionTime") for m in self.lr if m.which() == s]
      self.assertLess(min(ts), instant_max, f"high '{s}' execution time: {min(ts)}")
      self.assertLess(np.mean(ts), avg_max, f"high avg '{s}' execution time: {np.mean(ts)}")

  def test_timings(self):

    print("\n\n")
    print("="*25, "service timings", "="*25)
    for s, (maxmin, rsd) in TIMINGS.items():
      msgs = [m.logMonoTime for m in self.lr if m.which() == s]
      if not len(msgs):
        raise Exception(f"missing {s}")

      ts = np.diff(msgs) / 1e9
      dt = 1 / service_list[s].frequency

      np.testing.assert_allclose(np.mean(ts), dt, rtol=0.03, err_msg=f"{s} - failed mean timing check")
      np.testing.assert_allclose([np.max(ts), np.min(ts)], dt, rtol=maxmin, err_msg=f"{s} - failed max/min timing check")
      self.assertLess(np.std(ts) / dt, rsd, msg=f"{s} - failed RSD timing check")
      print(f"{s}: {np.array([np.mean(ts), np.max(ts), np.min(ts)])*1e3}")
      print(f"     {np.max(np.absolute([np.max(ts)/dt, np.min(ts)/dt]))} {np.std(ts)/dt}")
    print("="*67)

if __name__ == "__main__":
  unittest.main()
