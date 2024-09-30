#!/usr/bin/env python3
"""
Enforce more checks and controls for running daqbatch.
"""

from daq_utils import DaqManager, LOCALHOST
import argparse


def restartdaq(daqmgr, args):
    daqmgr.restartdaq(args.aimhost)


def wheredaq(daqmgr, args):
    daqmgr.wheredaq()


def stopdaq(daqmgr, args):
    daqmgr.stopdaq()


def isdaqbatch(daqmgr, args):
    daqmgr.isdaqbatch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="run_daq_utils", description=__doc__)

    parser.add_argument("-v", "--verbose", action="store_false")

    subparsers = parser.add_subparsers()
    psr_restart = subparsers.add_parser(
        "restartdaq",
        help="Verify requirements for running the daq then stop and start it.",
    )
    psr_restart.add_argument("-m", "--aimhost", action="store_const", const=LOCALHOST)
    psr_restart.set_defaults(func=restartdaq)

    psr_where = subparsers.add_parser(
        "wheredaq",
        help="Discover what host is running the daq in the current hutch, if any.",
    )
    psr_where.set_defaults(func=wheredaq)

    psr_stop = subparsers.add_parser(
        "stopdaq", help="Stop the daq in the current hutch."
    )
    psr_stop.set_defaults(func=stopdaq)

    psr_isdaqbatch = subparsers.add_parser(
        "isdaqbatch", help="Determine if the current hutch uses daqbatch"
    )
    psr_isdaqbatch.set_defaults(func=isdaqbatch)

    args = parser.parse_args()

    daqmgr = DaqManager(args.verbose)
    args.func(daqmgr, args)
