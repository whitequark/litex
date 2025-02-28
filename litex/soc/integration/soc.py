#
# This file is part of LiteX.
#
# This file is Copyright (c) 2014-2021 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2013-2014 Sebastien Bourdeauducq <sb@m-labs.hk>
# This file is Copyright (c) 2019 Gabriel L. Somlo <somlo@cmu.edu>
# SPDX-License-Identifier: BSD-2-Clause

import sys
import time
import logging
import datetime
from math import log2, ceil

from migen import *

from litex.soc.cores import cpu

from litex.soc.interconnect.csr import *
from litex.soc.interconnect.csr_eventmanager import *
from litex.soc.interconnect import csr_bus
from litex.soc.interconnect import stream
from litex.soc.interconnect import wishbone
from litex.soc.interconnect import axi

logging.basicConfig(level=logging.INFO)

# Helpers ------------------------------------------------------------------------------------------

def auto_int(x):
    return int(x, 0)

def colorer(s, color="bright"):
    header  = {
        "bright": "\x1b[1m",
        "green":  "\x1b[32m",
        "cyan":   "\x1b[36m",
        "red":    "\x1b[31m",
        "yellow": "\x1b[33m",
        "underline": "\x1b[4m"}[color]
    trailer = "\x1b[0m"
    return header + str(s) + trailer

def build_time(with_time=True):
    fmt = "%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d"
    return datetime.datetime.fromtimestamp(time.time()).strftime(fmt)

# SoCError -----------------------------------------------------------------------------------------

class SoCError(Exception):
    def __init__(self):
        sys.stderr = None # Error already described, avoid traceback/exception.

# SoCConstant --------------------------------------------------------------------------------------

def SoCConstant(value):
    return value

# SoCRegion ----------------------------------------------------------------------------------------

class SoCRegion:
    def __init__(self, origin=None, size=None, mode="rw", cached=True, linker=False):
        self.logger    = logging.getLogger("SoCRegion")
        self.origin    = origin
        self.size      = size
        if size != 2**log2_int(size, False):
            self.logger.info("Region size {} internally from {} to {}.".format(
                colorer("rounded", color="cyan"),
                colorer("0x{:08x}".format(size)),
                colorer("0x{:08x}".format(2**log2_int(size, False)))))
        self.size_pow2 = 2**log2_int(size, False)
        self.mode      = mode
        self.cached    = cached
        self.linker    = linker

    def decoder(self, bus):
        origin = self.origin
        size   = self.size_pow2
        if (origin & (size - 1)) != 0:
            self.logger.error("Origin needs to be aligned on size:")
            self.logger.error(self)
            raise SoCError()
        if (origin == 0) and (size == 2**bus.address_width):
            return lambda a : True
        origin >>= int(log2(bus.data_width//8)) # bytes to words aligned.
        size   >>= int(log2(bus.data_width//8)) # bytes to words aligned.
        return lambda a: (a[log2_int(size):] == (origin >> log2_int(size)))

    def __str__(self):
        r = ""
        if self.origin is not None:
            r += "Origin: {}, ".format(colorer("0x{:08x}".format(self.origin)))
        if self.size is not None:
            r += "Size: {}, ".format(colorer("0x{:08x}".format(self.size)))
        r += "Mode: {}, ".format(colorer(self.mode.upper()))
        r += "Cached: {} ".format(colorer(self.cached))
        r += "Linker: {}".format(colorer(self.linker))
        return r

class SoCIORegion(SoCRegion): pass

# SoCCSRRegion -------------------------------------------------------------------------------------

class SoCCSRRegion:
    def __init__(self, origin, busword, obj):
        self.origin  = origin
        self.busword = busword
        self.obj     = obj

# SoCBusHandler ------------------------------------------------------------------------------------

class SoCBusHandler(Module):
    supported_standard      = ["wishbone", "axi-lite"]
    supported_data_width    = [32, 64]
    supported_address_width = [32]

    # Creation -------------------------------------------------------------------------------------
    def __init__(self, name="SoCBusHandler", standard="wishbone", data_width=32, address_width=32, timeout=1e6, reserved_regions={}):
        self.logger = logging.getLogger(name)
        self.logger.info("Creating Bus Handler...")

        # Check Bus Standard.
        if standard not in self.supported_standard:
            self.logger.error("Unsupported {} {}, supporteds: {:s}".format(
                colorer("Bus standard", color="red"),
                colorer(standard),
                colorer(", ".join(self.supported_standard))))
            raise SoCError()

        # Check Bus Data Width.
        if data_width not in self.supported_data_width:
            self.logger.error("Unsupported {} {}, supporteds: {:s}".format(
                colorer("Data Width", color="red"),
                colorer(data_width),
                colorer(", ".join(str(x) for x in self.supported_data_width))))
            raise SoCError()

        # Check Bus Address Width.
        if address_width not in self.supported_address_width:
            self.logger.error("Unsupported {} {}, supporteds: {:s}".format(
                colorer("Address Width", color="red"),
                colorer(address_width),
                colorer(", ".join(str(x) for x in self.supported_address_width))))
            raise SoCError()

        # Create Bus
        self.standard      = standard
        self.data_width    = data_width
        self.address_width = address_width
        self.masters       = {}
        self.slaves        = {}
        self.regions       = {}
        self.io_regions    = {}
        self.timeout       = timeout
        self.logger.info("{}-bit {} Bus, {}GiB Address Space.".format(
            colorer(data_width), colorer(standard), colorer(2**address_width/2**30)))

        # Add reserved regions.
        self.logger.info("Adding {} Bus Regions...".format(colorer("reserved", color="cyan")))
        for name, region in reserved_regions.items():
            if isinstance(region, int):
                region = SoCRegion(origin=region, size=0x1000000)
            self.add_region(name, region)

        self.logger.info("Bus Handler {}.".format(colorer("created", color="green")))

    # Add/Allog/Check Regions ----------------------------------------------------------------------
    def add_region(self, name, region):
        allocated = False
        if name in self.regions.keys() or name in self.io_regions.keys():
            self.logger.error("{} already declared as Region:".format(colorer(name, color="red")))
            self.logger.error(self)
            raise SoCError()
        # Check if is SoCIORegion.
        if isinstance(region, SoCIORegion):
            self.io_regions[name] = region
            # Check for overlap with others IO regions.
            overlap = self.check_regions_overlap(self.io_regions)
            if overlap is not None:
                self.logger.error("IO Region {} between {} and {}:".format(
                    colorer("overlap", color="red"),
                    colorer(overlap[0]),
                    colorer(overlap[1])))
                self.logger.error(str(self.io_regions[overlap[0]]))
                self.logger.error(str(self.io_regions[overlap[1]]))
                raise SoCError()
            self.logger.info("{} Region {} at {}.".format(
                colorer(name,    color="underline"),
                colorer("added", color="green"),
                str(region)))
        # Check if is SoCRegion
        elif isinstance(region, SoCRegion):
            # If no Origin specified, allocate Region.
            if region.origin is None:
                allocated = True
                region    = self.alloc_region(name, region.size, region.cached)
                self.regions[name] = region
            # Else add Region.
            else:
                # If Region is an IO Region is not cached.
                if not region.cached:
                    if not self.check_region_is_io(region):
                        self.logger.error("{} Region {}: {}.".format(
                            colorer(name),
                            colorer("not in IO region", color="red"),
                            str(region)))
                        self.logger.error(self)
                        raise SoCError()
                self.regions[name] = region
                # Check for overlab with others IO regions.
                overlap = self.check_regions_overlap(self.regions)
                if overlap is not None:
                    self.logger.error("Region {} between {} and {}:".format(
                        colorer("overlap", color="red"),
                        colorer(overlap[0]),
                        colorer(overlap[1])))
                    self.logger.error(str(self.regions[overlap[0]]))
                    self.logger.error(str(self.regions[overlap[1]]))
                    raise SoCError()
            self.logger.info("{} Region {} at {}.".format(
                colorer(name, color="underline"),
                colorer("allocated" if allocated else "added", color="cyan" if allocated else "green"),
                str(region)))
        else:
            self.logger.error("{} is not a supported Region.".format(colorer(name, color="red")))
            raise SoCError()

    def alloc_region(self, name, size, cached=True):
        self.logger.info("Allocating {} Region of size {}...".format(
            colorer("Cached" if cached else "IO"),
            colorer("0x{:08x}".format(size))))

        # Limit Search Regions.
        if cached == False:
            search_regions = self.io_regions
        else:
            search_regions = {"main": SoCRegion(origin=0x00000000, size=2**self.address_width-1)}

        # Iterate on Search_Regions to find a Candidate.
        for _, search_region in search_regions.items():
            origin = search_region.origin
            while (origin + size) < (search_region.origin + search_region.size_pow2):
                # Align Origin on Size.
                if (origin%size):
                    origin += (origin - origin%size)
                    continue
                # Create a Candidate.
                candidate = SoCRegion(origin=origin, size=size, cached=cached)
                overlap   = False
                # Check Candidate does not overlap with allocated existing regions.
                for _, allocated in self.regions.items():
                    if self.check_regions_overlap({"0": allocated, "1": candidate}) is not None:
                        origin += size
                        overlap = True
                        break
                if not overlap:
                    # If no overlap, the Candidate is selected.
                    return candidate

        self.logger.error("Not enough Address Space to allocate Region.")
        raise SoCError()

    def check_regions_overlap(self, regions, check_linker=False):
        i = 0
        while i < len(regions):
            n0 =  list(regions.keys())[i]
            r0 = regions[n0]
            for n1 in list(regions.keys())[i+1:]:
                r1 = regions[n1]
                if r0.linker or r1.linker:
                    if not check_linker:
                        continue
                if r0.origin >= (r1.origin + r1.size_pow2):
                    continue
                if r1.origin >= (r0.origin + r0.size_pow2):
                    continue
                return (n0, n1)
            i += 1
        return None

    def check_region_is_in(self, region, container):
        is_in = True
        if not (region.origin >= container.origin):
            is_in = False
        if not ((region.origin + region.size) < (container.origin + container.size)):
            is_in = False
        return is_in

    def check_region_is_io(self, region):
        is_io = False
        for _, io_region in self.io_regions.items():
            if self.check_region_is_in(region, io_region):
                is_io = True
        return is_io

    # Add Master/Slave -----------------------------------------------------------------------------
    def add_adapter(self, name, interface, direction="m2s"):
        assert direction in ["m2s", "s2m"]

        # Data width conversion.
        if interface.data_width != self.data_width:
            interface_cls = type(interface)
            converter_cls = {
                wishbone.Interface:   wishbone.Converter,
                axi.AXILiteInterface: axi.AXILiteConverter,
            }[interface_cls]
            converted_interface = interface_cls(data_width=self.data_width)
            if direction == "m2s":
                master, slave = interface, converted_interface
            elif direction == "s2m":
                master, slave = converted_interface, interface
            converter = converter_cls(master=master, slave=slave)
            self.submodules += converter
        else:
            converted_interface = interface

        # Wishbone <-> AXILite bridging.
        main_bus_cls = {
            "wishbone": wishbone.Interface,
            "axi-lite": axi.AXILiteInterface,
        }[self.standard]
        if isinstance(converted_interface, main_bus_cls):
            bridged_interface = converted_interface
        else:
            bridged_interface = main_bus_cls(data_width=self.data_width)
            if direction == "m2s":
                master, slave = converted_interface, bridged_interface
            elif direction == "s2m":
                master, slave = bridged_interface, converted_interface
            bridge_cls = {
                (wishbone.Interface, axi.AXILiteInterface): axi.Wishbone2AXILite,
                (axi.AXILiteInterface, wishbone.Interface): axi.AXILite2Wishbone,
            }[type(master), type(slave)]
            bridge = bridge_cls(master, slave)
            self.submodules += bridge

        if type(interface) != type(bridged_interface) or interface.data_width != bridged_interface.data_width:
            fmt = "{name} Bus {converted} from {from_bus} {from_bits}-bit to {to_bus} {to_bits}-bit."
            bus_names = {
                wishbone.Interface:   "Wishbone",
                axi.AXILiteInterface: "AXI Lite",
            }
            self.logger.info(fmt.format(
                name      = colorer(name),
                converted = colorer("converted", color="cyan"),
                from_bus  = colorer(bus_names[type(interface)]),
                from_bits = colorer(interface.data_width),
                to_bus    = colorer(bus_names[type(bridged_interface)]),
                to_bits   = colorer(bridged_interface.data_width)))
        return bridged_interface

    def add_master(self, name=None, master=None):
        if name is None:
            name = "master{:d}".format(len(self.masters))
        if name in self.masters.keys():
            self.logger.error("{} {} as Bus Master:".format(
                colorer(name),
                colorer("already declared", color="red")))
            self.logger.error(self)
            raise SoCError()
        master = self.add_adapter(name, master, "m2s")
        self.masters[name] = master
        self.logger.info("{} {} as Bus Master.".format(
            colorer(name,    color="underline"),
            colorer("added", color="green")))

    def add_slave(self, name=None, slave=None, region=None):
        no_name   = name is None
        no_region = region is None
        if no_name and no_region:
            self.logger.error("Please {} {} or/and {} of Bus Slave.".format(
                colorer("specify", color="red"),
                colorer("name"),
                colorer("region")))
            raise SoCError()
        if no_name:
            name = "slave{:d}".format(len(self.slaves))
        if no_region:
            region = self.regions.get(name, None)
            if region is None:
                self.logger.error("{} Region {}.".format(
                    colorer(name),
                    colorer("not found", color="red")))
                raise SoCError()
        else:
             self.add_region(name, region)
        if name in self.slaves.keys():
            self.logger.error("{} {} as Bus Slave:".format(
                colorer(name),
                colorer("already declared", color="red")))
            self.logger.error(self)
            raise SoCError()
        slave = self.add_adapter(name, slave, "s2m")
        self.slaves[name] = slave
        self.logger.info("{} {} as Bus Slave.".format(
            colorer(name, color="underline"),
            colorer("added", color="green")))

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{}-bit {} Bus, {}GiB Address Space.\n".format(
            colorer(self.data_width), colorer(self.standard), colorer(2**self.address_width/2**30))
        r += "IO Regions: ({})\n".format(len(self.io_regions.keys())) if len(self.io_regions.keys()) else ""
        io_regions = {k: v for k, v in sorted(self.io_regions.items(), key=lambda item: item[1].origin)}
        for name, region in io_regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Bus Regions: ({})\n".format(len(self.regions.keys())) if len(self.regions.keys()) else ""
        regions = {k: v for k, v in sorted(self.regions.items(), key=lambda item: item[1].origin)}
        for name, region in regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Bus Masters: ({})\n".format(len(self.masters.keys())) if len(self.masters.keys()) else ""
        for name in self.masters.keys():
           r += "- {}\n".format(colorer(name, color="underline"))
        r += "Bus Slaves: ({})\n".format(len(self.slaves.keys())) if len(self.slaves.keys()) else ""
        for name in self.slaves.keys():
           r += "- {}\n".format(colorer(name, color="underline"))
        r = r[:-1]
        return r

# SoCLocHandler ------------------------------------------------------------------------------------

class SoCLocHandler(Module):
    # Creation -------------------------------------------------------------------------------------
    def __init__(self, name, n_locs):
        self.name   = name
        self.locs   = {}
        self.n_locs = n_locs

    # Add ------------------------------------------------------------------------------------------
    def add(self, name, n=None, use_loc_if_exists=False):
        allocated = False
        if not (use_loc_if_exists and name in self.locs.keys()):
            if name in self.locs.keys():
                self.logger.error("{} {} name {}.".format(
                    colorer(name), self.name, colorer("already used", color="red")))
                self.logger.error(self)
                raise SoCError()
            if n in self.locs.values():
                self.logger.error("{} {} Location {}.".format(
                    colorer(n), self.name, colorer("already used", color="red")))
                self.logger.error(self)
                raise SoCError()
            if n is None:
                allocated = True
                n = self.alloc(name)
            else:
                if n < 0:
                    self.logger.error("{} {} Location should be {}.".format(
                        colorer(n),
                        self.name,
                        colorer("positive", color="red")))
                    raise SoCError()
                if n > self.n_locs:
                    self.logger.error("{} {} Location {} than maximum: {}.".format(
                        colorer(n),
                        self.name,
                        colorer("higher", color="red"),
                        colorer(self.n_locs)))
                    raise SoCError()
            self.locs[name] = n
        else:
            n = self.locs[name]
        self.logger.info("{} {} {} at Location {}.".format(
            colorer(name, color="underline"),
            self.name,
            colorer("allocated" if allocated else "added", color="cyan" if allocated else "green"),
            colorer(n)))

    # Alloc ----------------------------------------------------------------------------------------
    def alloc(self, name):
        for n in range(self.n_locs):
            if n not in self.locs.values():
                return n
        self.logger.error("Not enough Locations.")
        self.logger.error(self)
        raise SoCError()

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{} Locations: ({})\n".format(self.name, len(self.locs.keys())) if len(self.locs.keys()) else ""
        locs = {k: v for k, v in sorted(self.locs.items(), key=lambda item: item[1])}
        length = 0
        for name in locs.keys():
            if len(name) > length: length = len(name)
        for name in locs.keys():
           r += "- {}{}: {}\n".format(colorer(name, color="underline"), " "*(length + 1 - len(name)), colorer(self.locs[name]))
        return r

# SoCCSRHandler ------------------------------------------------------------------------------------

class SoCCSRHandler(SoCLocHandler):
    supported_data_width    = [8, 32]
    supported_address_width = [14+i for i in range(4)]
    supported_alignment     = [32]
    supported_paging        = [0x800*2**i for i in range(4)]
    supported_ordering      = ["big", "little"]

    # Creation -------------------------------------------------------------------------------------
    def __init__(self, data_width=32, address_width=14, alignment=32, paging=0x800, ordering="big", reserved_csrs={}):
        SoCLocHandler.__init__(self, "CSR", n_locs=alignment//8*(2**address_width)//paging)
        self.logger = logging.getLogger("SoCCSRHandler")
        self.logger.info("Creating CSR Handler...")

        # Check CSR Data Width.
        if data_width not in self.supported_data_width:
            self.logger.error("Unsupported {} {}, supporteds: {:s}".format(
                colorer("Data Width", color="red"),
                colorer(data_width),
                colorer(", ".join(str(x) for x in self.supported_data_width))))
            raise SoCError()

        # Check CSR Address Width.
        if address_width not in self.supported_address_width:
            self.logger.error("Unsupported {} {} supporteds: {:s}".format(
                colorer("Address Width", color="red"),
                colorer(address_width),
                colorer(", ".join(str(x) for x in self.supported_address_width))))
            raise SoCError()

        # Check CSR Alignment.
        if alignment not in self.supported_alignment:
            self.logger.error("Unsupported {}: {} supporteds: {:s}".format(
                colorer("Alignment", color="red"),
                colorer(alignment),
                colorer(", ".join(str(x) for x in self.supported_alignment))))
            raise SoCError()
        if data_width > alignment:
            self.logger.error("Alignment ({}) {} Data Width ({})".format(
                colorer(alignment),
                colorer("should be >=", color="red"),
                colorer(data_width)))
            raise SoCError()

        # Check CSR Paging.
        if paging not in self.supported_paging:
            self.logger.error("Unsupported {} 0x{}, supporteds: {:s}".format(
                colorer("Paging", color="red"),
                colorer("{:x}".format(paging)),
                colorer(", ".join("0x{:x}".format(x) for x in self.supported_paging))))
            raise SoCError()

        # Check CSR Ordering.
        if ordering not in self.supported_ordering:
            self.logger.error("Unsupported {} {}, supporteds: {:s}".format(
                colorer("Ordering", color="red"),
                colorer("{}".format(paging)),
                colorer(", ".join("{}".format(x) for x in self.supported_ordering))))
            raise SoCError()

        # Create CSR Handler.
        self.data_width    = data_width
        self.address_width = address_width
        self.alignment     = alignment
        self.paging        = paging
        self.ordering      = ordering
        self.masters       = {}
        self.regions       = {}
        self.logger.info("{}-bit CSR Bus, {}-bit Aligned, {}KiB Address Space, {}B Paging, {} Ordering (Up to {} Locations).".format(
            colorer(self.data_width),
            colorer(self.alignment),
            colorer(2**self.address_width/2**10),
            colorer(self.paging),
            colorer(self.ordering),
            colorer(self.n_locs)))

        # Add reserved CSRs.
        self.logger.info("Adding {} CSRs...".format(colorer("reserved", color="cyan")))
        for name, n in reserved_csrs.items():
            self.add(name, n)

        self.logger.info("CSR Handler {}.".format(colorer("created", color="green")))

    # Add Master -----------------------------------------------------------------------------------
    def add_master(self, name=None, master=None):
        if name is None:
            name = "master{:d}".format(len(self.masters))
        if name in self.masters.keys():
            self.logger.error("{} {} as CSR Master:".format(
                colorer(name),
                colorer("already declared", color="red")))
            self.logger.error(self)
            raise SoCError()
        if master.data_width != self.data_width:
            self.logger.error("{} Master/Handler Data Width {} ({} vs {}).".format(
                colorer(name),
                colorer("missmatch", color="red"),
                colorer(master.data_width),
                colorer(self.data_width)))
            raise SoCError()
        self.masters[name] = master
        self.logger.info("{} {} as CSR Master.".format(
            colorer(name,    color="underline"),
            colorer("added", color="green")))

    # Add Region -----------------------------------------------------------------------------------
    def add_region(self, name, region):
        # FIXME: add checks
        self.regions[name] = region

    # Address map ----------------------------------------------------------------------------------
    def address_map(self, name, memory):
        if memory is not None:
            name = name + "_" + memory.name_override
        if self.locs.get(name, None) is None:
            self.add(name, use_loc_if_exists=True)
        return self.locs[name]

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{}-bit CSR Bus, {}-bit Aligned, {}KiB Address Space, {}B Paging, {} Ordering (Up to {} Locations).\n".format(
            colorer(self.data_width),
            colorer(self.alignment),
            colorer(2**self.address_width/2**10),
            colorer(self.paging),
            colorer(self.ordering),
            colorer(self.n_locs))
        r += SoCLocHandler.__str__(self)
        r = r[:-1]
        return r

# SoCIRQHandler ------------------------------------------------------------------------------------

class SoCIRQHandler(SoCLocHandler):
    # Creation -------------------------------------------------------------------------------------
    def __init__(self, n_irqs=32, reserved_irqs={}):
        SoCLocHandler.__init__(self, "IRQ", n_locs=n_irqs)
        self.logger = logging.getLogger("SoCIRQHandler")
        self.logger.info("Creating IRQ Handler...")
        self.enabled = False

        # Check IRQ Number.
        if n_irqs > 32:
            self.logger.error("Unsupported IRQs number: {} supporteds: {:s}".format(
                colorer(n_irqs, color="red"), colorer("Up to 32", color="green")))
            raise SoCError()

        # Create IRQ Handler.
        self.logger.info("IRQ Handler (up to {} Locations).".format(colorer(n_irqs)))

        # Adding reserved IRQs.
        self.logger.info("Adding {} IRQs...".format(colorer("reserved", color="cyan")))
        for name, n in reserved_irqs.items():
            self.add(name, n)

        self.logger.info("IRQ Handler {}.".format(colorer("created", color="green")))

    # Enable ---------------------------------------------------------------------------------------
    def enable(self):
        self.enabled = True

    # Add ------------------------------------------------------------------------------------------
    def add(self, name, *args, **kwargs):
        if self.enabled:
            SoCLocHandler.add(self, name, *args, **kwargs)
        else:
            self.logger.error("Attempted to add {} IRQ but SoC does {}.".format(
                colorer(name), colorer("not support IRQs", color="red")))
            raise SoCError()

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r ="IRQ Handler (up to {} Locations).\n".format(colorer(self.n_locs))
        r += SoCLocHandler.__str__(self)
        r = r[:-1]
        return r

# SoCController ------------------------------------------------------------------------------------

class SoCController(Module, AutoCSR):
    def __init__(self, with_reset=True, with_scratch=True, with_errors=True):
        if with_reset:
            self._reset = CSRStorage(fields=[
                CSRField("soc_rst", size=1, offset=0, pulse=True, description="""Write `1` to this register to reset the full SoC (Pulse Reset)"""),
                CSRField("cpu_rst", size=1, offset=1,             description="""Write `1` to this register to reset the CPU(s) of the SoC (Hold Reset)"""),
            ])
        if with_scratch:
            self._scratch = CSRStorage(32, reset=0x12345678, description="""
                Use this register as a scratch space to verify that software read/write accesses
                to the Wishbone/CSR bus are working correctly. The initial reset value of 0x1234578
                can be used to verify endianness.""")
        if with_errors:
            self._bus_errors = CSRStatus(32, description="Total number of Wishbone bus errors (timeouts) since start.")

        # # #

        # Reset
        if with_reset:
            self.soc_rst = self._reset.fields.soc_rst
            self.cpu_rst = self._reset.fields.cpu_rst

        # Errors
        if with_errors:
            self.bus_error = Signal()
            bus_errors     = Signal(32)
            self.sync += [
                If(bus_errors != (2**len(bus_errors)-1),
                    If(self.bus_error, bus_errors.eq(bus_errors + 1))
                )
            ]
            self.comb += self._bus_errors.status.eq(bus_errors)

# SoC ----------------------------------------------------------------------------------------------

class SoC(Module):
    mem_map = {}
    def __init__(self, platform, sys_clk_freq,
        bus_standard         = "wishbone",
        bus_data_width       = 32,
        bus_address_width    = 32,
        bus_timeout          = 1e6,
        bus_reserved_regions = {},

        csr_data_width       = 32,
        csr_address_width    = 14,
        csr_paging           = 0x800,
        csr_ordering         = "big",
        csr_reserved_csrs    = {},

        irq_n_irqs           = 32,
        irq_reserved_irqs    = {},
        ):

        self.logger = logging.getLogger("SoC")
        self.logger.info(colorer("        __   _ __      _  __  ", color="bright"))
        self.logger.info(colorer("       / /  (_) /____ | |/_/  ", color="bright"))
        self.logger.info(colorer("      / /__/ / __/ -_)>  <    ", color="bright"))
        self.logger.info(colorer("     /____/_/\\__/\\__/_/|_|  ", color="bright"))
        self.logger.info(colorer("  Build your hardware, easily!", color="bright"))

        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Creating SoC... ({})".format(build_time())))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info("FPGA device : {}.".format(platform.device))
        self.logger.info("System clock: {:3.3f}MHz.".format(sys_clk_freq/1e6))

        # SoC attributes ---------------------------------------------------------------------------
        self.platform     = platform
        self.sys_clk_freq = sys_clk_freq
        self.constants    = {}
        self.csr_regions  = {}

        # SoC Bus Handler --------------------------------------------------------------------------
        self.submodules.bus = SoCBusHandler(
            standard         = bus_standard,
            data_width       = bus_data_width,
            address_width    = bus_address_width,
            timeout          = bus_timeout,
            reserved_regions = bus_reserved_regions,
           )

        # SoC Bus Handler --------------------------------------------------------------------------
        self.submodules.csr = SoCCSRHandler(
            data_width    = csr_data_width,
            address_width = csr_address_width,
            alignment     = 32,
            paging        = csr_paging,
            ordering      = csr_ordering,
            reserved_csrs = csr_reserved_csrs,
        )

        # SoC IRQ Handler --------------------------------------------------------------------------
        self.submodules.irq = SoCIRQHandler(
            n_irqs        = irq_n_irqs,
            reserved_irqs = irq_reserved_irqs
        )

        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Initial SoC:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(self.bus)
        self.logger.info(self.csr)
        self.logger.info(self.irq)
        self.logger.info(colorer("-"*80, color="bright"))

        self.add_config("CLOCK_FREQUENCY", int(sys_clk_freq))

    # SoC Helpers ----------------------------------------------------------------------------------
    def check_if_exists(self, name):
        if hasattr(self, name):
            self.logger.error("{} SubModule already {}.".format(
                colorer(name),
                colorer("declared", color="red")))
            raise SoCError()

    def add_constant(self, name, value=None):
        name = name.upper()
        if name in self.constants.keys():
            self.logger.error("{} Constant already {}.".format(
                colorer(name),
                colorer("declared", color="red")))
            raise SoCError()
        self.constants[name] = SoCConstant(value)

    def add_config(self, name, value=None):
        name = "CONFIG_" + name
        if isinstance(value, str):
            self.add_constant(name + "_" + value)
        else:
            self.add_constant(name, value)

    def check_bios_requirements(self):
        # Check for required Peripherals.
        for periph in [ "timer0"]:
            if periph not in self.csr.locs.keys():
                self.logger.error("BIOS needs {} peripheral to be {}.".format(
                    colorer(periph),
                    colorer("used", color="red")))
                self.logger.error(self.bus)
                raise SoCError()

        # Check for required Memory Regions.
        for mem in ["rom", "sram"]:
            if mem not in self.bus.regions.keys():
                self.logger.error("BIOS needs {} Region to be {} as Bus or Linker Region.".format(
                    colorer(mem),
                    colorer("defined", color="red")))
                self.logger.error(self.bus)
                raise SoCError()

    # SoC Main Components --------------------------------------------------------------------------
    def add_controller(self, name="ctrl", **kwargs):
        self.check_if_exists(name)
        setattr(self.submodules, name, SoCController(**kwargs))

    def add_ram(self, name, origin, size, contents=[], mode="rw"):
        ram_cls = {
            "wishbone": wishbone.SRAM,
            "axi-lite": axi.AXILiteSRAM,
        }[self.bus.standard]
        interface_cls = {
            "wishbone": wishbone.Interface,
            "axi-lite": axi.AXILiteInterface,
        }[self.bus.standard]
        ram_bus = interface_cls(data_width=self.bus.data_width)
        ram     = ram_cls(size, bus=ram_bus, init=contents, read_only=(mode == "r"))
        self.bus.add_slave(name, ram.bus, SoCRegion(origin=origin, size=size, mode=mode))
        self.check_if_exists(name)
        self.logger.info("RAM {} {} {}.".format(
            colorer(name),
            colorer("added", color="green"),
            self.bus.regions[name]))
        setattr(self.submodules, name, ram)

    def add_rom(self, name, origin, size, contents=[], mode="r"):
        self.add_ram(name, origin, size, contents, mode=mode)

    def init_rom(self, name, contents=[], auto_size=True):
        self.logger.info("Initializing ROM {} with contents (Size: {}).".format(
            colorer(name),
            colorer(f"0x{4*len(contents):x}")))
        getattr(self, name).mem.init = contents
        if auto_size and self.bus.regions[name].mode == "r":
            self.logger.info("Auto-Resizing ROM {} from {} to {}.".format(
                colorer(name),
                colorer(f"0x{self.bus.regions[name].size:x}"),
                colorer(f"0x{4*len(contents):x}")))
            getattr(self, name).mem.depth = len(contents)

    def add_csr_bridge(self, origin, register=False):
        csr_bridge_cls = {
            "wishbone": wishbone.Wishbone2CSR,
            "axi-lite": axi.AXILite2CSR,
        }[self.bus.standard]
        self.check_if_exists("csr_bridge")
        self.submodules.csr_bridge = csr_bridge_cls(
            bus_csr       = csr_bus.Interface(
            address_width = self.csr.address_width,
            data_width    = self.csr.data_width),
            register      = register)
        csr_size   = 2**(self.csr.address_width + 2)
        csr_region = SoCRegion(origin=origin, size=csr_size, cached=False)
        bus = getattr(self.csr_bridge, self.bus.standard.replace('-', '_'))
        self.bus.add_slave("csr", bus, csr_region)
        self.csr.add_master(name="bridge", master=self.csr_bridge.csr)
        self.add_config("CSR_DATA_WIDTH", self.csr.data_width)
        self.add_config("CSR_ALIGNMENT",  self.csr.alignment)

    def add_cpu(self, name="vexriscv", variant="standard", cls=None, reset_address=None, cfu=None):
        # Check that CPU is supported.
        if name not in cpu.CPUS.keys():
            self.logger.error("{} CPU {}, supporteds: {}.".format(
                colorer(name),
                colorer("not supported", color="red"),
                colorer(", ".join(cpu.CPUS.keys()))))
            raise SoCError()

        # Add CPU.
        if name == "external" and cls is None:
            self.logger.error("{} CPU requires {} to be specified.".format(
                colorer(name),
                colorer("cpu_cls", color="red")))
            raise SoCError()
        cpu_cls = cls if cls is not None else cpu.CPUS[name]
        if (variant not in cpu_cls.variants) and (cpu_cls is not cpu.CPUNone):
            self.logger.error("{} CPU variant {}, supporteds: {}.".format(
                colorer(variant),
                colorer("not supported", color="red"),
                colorer(", ".join(cpu_cls.variants))))
            raise SoCError()
        self.check_if_exists("cpu")
        self.submodules.cpu = cpu_cls(self.platform, variant)

        # Add optional CFU plugin.
        if "cfu" in variant and hasattr(self.cpu, "add_cfu"):
            self.cpu.add_cfu(cfu_filename=cfu)

        # Update SoC with CPU constraints.
        # IOs regions.
        for n, (origin, size) in enumerate(self.cpu.io_regions.items()):
            self.bus.add_region("io{}".format(n), SoCIORegion(origin=origin, size=size, cached=False))
        # Mapping.
        if isinstance(self.cpu, cpu.CPUNone):
            # With CPUNone, give priority to User's mapping.
            self.mem_map = {**self.cpu.mem_map, **self.mem_map}
        else:
            # Override User's mapping with CPU constrainted mapping (and warn User).
            for n, origin in self.cpu.mem_map.items():
                if n in self.mem_map.keys():
                    self.logger.info("CPU {} {} mapping from {} to {}.".format(
                        colorer("overriding", color="cyan"),
                        colorer(n),
                        colorer(f"0x{self.mem_map[n]:x}"),
                        colorer(f"0x{self.cpu.mem_map[n]:x}")))
            self.mem_map.update(self.cpu.mem_map)

        # Add Bus Masters/CSR/IRQs.
        if not isinstance(self.cpu, (cpu.CPUNone, cpu.Zynq7000)):
            if hasattr(self.cpu, "set_reset_address"):
                if reset_address is None:
                    reset_address = self.mem_map["rom"]
                self.cpu.set_reset_address(reset_address)
            for n, cpu_bus in enumerate(self.cpu.periph_buses):
                self.bus.add_master(name="cpu_bus{}".format(n), master=cpu_bus)
            if hasattr(self.cpu, "interrupt"):
                self.irq.enable()
                for name, loc in self.cpu.interrupts.items():
                    self.irq.add(name, loc)
                self.add_config("CPU_HAS_INTERRUPT")

            # Create optional DMA Bus (for Cache Coherence).
            if hasattr(self.cpu, "dma_bus"):
                self.submodules.dma_bus = SoCBusHandler(
                    name             = "SoCDMABusHandler",
                    standard         = "wishbone",
                    data_width       = self.bus.data_width,
                    address_width    = self.bus.address_width,
                )
                dma_bus = wishbone.Interface(data_width=self.bus.data_width)
                self.dma_bus.add_slave("dma", slave=dma_bus, region=SoCRegion(origin=0x00000000, size=0x100000000)) # FIXME: covers lower 4GB only
                self.submodules += wishbone.Converter(dma_bus, self.cpu.dma_bus)

            # Connect SoCController's reset to CPU reset.
            if hasattr(self, "ctrl"):
                self.comb += self.cpu.reset.eq(
                    # Reset the CPU on...
                    getattr(self.ctrl, "soc_rst", 0) | # Full SoC Reset command...
                    getattr(self.ctrl, "cpu_rst", 0)   # or on CPU Reset command.
                )
            self.add_config("CPU_RESET_ADDR", reset_address)

        # Add CPU's SoC components (if any).
        if hasattr(self.cpu, "add_soc_components"):
            self.cpu.add_soc_components(soc=self, soc_region_cls=SoCRegion) # FIXME: avoid passing SoCRegion.

        # Add constants.
        self.add_config("CPU_TYPE",    str(name))
        self.add_config("CPU_VARIANT", str(variant.split('+')[0]))
        self.add_constant("CONFIG_CPU_HUMAN_NAME", getattr(self.cpu, "human_name", "Unknown"))
        if hasattr(self.cpu, "nop"):
            self.add_constant("CONFIG_CPU_NOP", self.cpu.nop)

    def add_timer(self, name="timer0"):
        from litex.soc.cores.timer import Timer
        self.check_if_exists(name)
        setattr(self.submodules, name, Timer())
        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

    # SoC finalization -----------------------------------------------------------------------------
    def do_finalize(self):
        interconnect_p2p_cls = {
            "wishbone": wishbone.InterconnectPointToPoint,
            "axi-lite": axi.AXILiteInterconnectPointToPoint,
        }[self.bus.standard]
        interconnect_shared_cls = {
            "wishbone": wishbone.InterconnectShared,
            "axi-lite": axi.AXILiteInterconnectShared,
        }[self.bus.standard]

        # SoC Reset --------------------------------------------------------------------------------
        # Connect soc_rst to CRG's rst if presents.
        if hasattr(self, "ctrl") and hasattr(self, "crg"):
            crg_rst = getattr(self.crg, "rst", None)
            if isinstance(crg_rst, Signal):
                self.comb += crg_rst.eq(getattr(self.ctrl, "soc_rst", 0))

        # SoC CSR bridge ---------------------------------------------------------------------------
        # FIXME: for now, use registered CSR bridge when SDRAM is present; find the best compromise.
        self.add_csr_bridge(self.mem_map["csr"], register=hasattr(self, "sdram"))

        # SoC Bus Interconnect ---------------------------------------------------------------------
        if len(self.bus.masters) and len(self.bus.slaves):
            # If 1 bus_master, 1 bus_slave and no address translation, use InterconnectPointToPoint.
            if ((len(self.bus.masters) == 1)  and
                (len(self.bus.slaves)  == 1)  and
                (next(iter(self.bus.regions.values())).origin == 0)):
                self.submodules.bus_interconnect = interconnect_p2p_cls(
                    master = next(iter(self.bus.masters.values())),
                    slave  = next(iter(self.bus.slaves.values())))
            # Otherwise, use InterconnectShared.
            else:
                self.submodules.bus_interconnect = interconnect_shared_cls(
                    masters        = self.bus.masters.values(),
                    slaves         = [(self.bus.regions[n].decoder(self.bus), s) for n, s in self.bus.slaves.items()],
                    register       = True,
                    timeout_cycles = self.bus.timeout)
                if hasattr(self, "ctrl") and self.bus.timeout is not None:
                    if hasattr(self.ctrl, "bus_error"):
                        self.comb += self.ctrl.bus_error.eq(self.bus_interconnect.timeout.error)
            self.bus.logger.info("Interconnect: {} ({} <-> {}).".format(
                colorer(self.bus_interconnect.__class__.__name__),
                colorer(len(self.bus.masters)),
                colorer(len(self.bus.slaves))))
        self.add_constant("CONFIG_BUS_STANDARD",      self.bus.standard.upper())
        self.add_constant("CONFIG_BUS_DATA_WIDTH",    self.bus.data_width)
        self.add_constant("CONFIG_BUS_ADDRESS_WIDTH", self.bus.address_width)

        # SoC DMA Bus Interconnect (Cache Coherence) -----------------------------------------------
        if hasattr(self, "dma_bus"):
            if len(self.dma_bus.masters) and len(self.dma_bus.slaves):
                # If 1 bus_master, 1 bus_slave and no address translation, use InterconnectPointToPoint.
                if ((len(self.dma_bus.masters) == 1)  and
                    (len(self.dma_bus.slaves)  == 1)  and
                    (next(iter(self.dma_bus.regions.values())).origin == 0)):
                    self.submodules.dma_bus_interconnect = wishbone.InterconnectPointToPoint(
                        master = next(iter(self.dma_bus.masters.values())),
                        slave  = next(iter(self.dma_bus.slaves.values())))
                # Otherwise, use InterconnectShared.
                else:
                    self.submodules.dma_bus_interconnect = wishbone.InterconnectShared(
                        masters        = self.dma_bus.masters.values(),
                        slaves         = [(self.dma_bus.regions[n].decoder(self.dma_bus), s) for n, s in self.dma_bus.slaves.items()],
                        register       = True)
                self.bus.logger.info("DMA Interconnect: {} ({} <-> {}).".format(
                    colorer(self.dma_bus_interconnect.__class__.__name__),
                    colorer(len(self.dma_bus.masters)),
                    colorer(len(self.dma_bus.slaves))))
            self.add_constant("CONFIG_CPU_HAS_DMA_BUS")

        # SoC CSR Interconnect ---------------------------------------------------------------------
        self.submodules.csr_bankarray = csr_bus.CSRBankArray(self,
            address_map        = self.csr.address_map,
            data_width         = self.csr.data_width,
            address_width      = self.csr.address_width,
            alignment          = self.csr.alignment,
            paging             = self.csr.paging,
            ordering           = self.csr.ordering,
            soc_bus_data_width = self.bus.data_width)
        if len(self.csr.masters):
            self.submodules.csr_interconnect = csr_bus.InterconnectShared(
                masters = list(self.csr.masters.values()),
                slaves  = self.csr_bankarray.get_buses())

        # Add CSRs regions.
        for name, csrs, mapaddr, rmap in self.csr_bankarray.banks:
            self.csr.add_region(name, SoCCSRRegion(
                origin   = (self.bus.regions["csr"].origin + self.csr.paging*mapaddr),
                busword  = self.csr.data_width,
                obj      = csrs))

        # Add Memory regions.
        for name, memory, mapaddr, mmap in self.csr_bankarray.srams:
            self.csr.add_region(name + "_" + memory.name_override, SoCCSRRegion(
                origin  = (self.bus.regions["csr"].origin + self.csr.paging*mapaddr),
                busword = self.csr.data_width,
                obj     = memory))

        # Sort CSR regions by origin.
        self.csr.regions = {k: v for k, v in sorted(self.csr.regions.items(), key=lambda item: item[1].origin)}

        # Add CSRs / Config items to constants.
        for name, constant in self.csr_bankarray.constants:
            self.add_constant(name + "_" + constant.name, constant.value.value)

        # SoC CPU Check ----------------------------------------------------------------------------
        if not isinstance(self.cpu, (cpu.CPUNone, cpu.Zynq7000, cpu.EOS_S3)):
            cpu_reset_address_valid = False
            for name, container in self.bus.regions.items():
                if self.bus.check_region_is_in(
                    region    = SoCRegion(origin=self.cpu.reset_address, size=self.bus.data_width//8),
                    container = container):
                    cpu_reset_address_valid = True
                    if name == "rom":
                        self.cpu.use_rom = True
            if not cpu_reset_address_valid:
                self.logger.error("CPU needs {} to be in a {} Region.".format(
                    colorer("reset address 0x{:08x}".format(self.cpu.reset_address)),
                    colorer("defined", color="red")))
                self.logger.error(self.bus)
                raise SoCError()

        # SoC IRQ Interconnect ---------------------------------------------------------------------
        if hasattr(self, "cpu") and hasattr(self.cpu, "interrupt"):
            for name, loc in sorted(self.irq.locs.items()):
                if name in self.cpu.interrupts.keys():
                    continue
                if hasattr(self, name):
                    module = getattr(self, name)
                    ev = None
                    if hasattr(module, "ev"):
                        ev = module.ev
                    elif isinstance(module, EventManager):
                        ev = module
                    else:
                        self.logger.error("EventManager {} in {} SubModule.".format(
                            colorer("not found", color="red"),
                            colorer(name)))
                        raise SoCError()
                    self.comb += self.cpu.interrupt[loc].eq(ev.irq)
                self.add_constant(name + "_INTERRUPT", loc)

        # SoC Infos --------------------------------------------------------------------------------
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Finalized SoC:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(self.bus)
        if hasattr(self, "dma_bus"):
            self.logger.info(self.dma_bus)
        self.logger.info(self.csr)
        self.logger.info(self.irq)
        self.logger.info(colorer("-"*80, color="bright"))

    # SoC build ------------------------------------------------------------------------------------
    def build(self, *args, **kwargs):
        self.build_name = kwargs.pop("build_name", self.platform.name)
        if self.build_name[0].isdigit():
            self.build_name = f"_{self.build_name}"
        kwargs.update({"build_name": self.build_name})
        return self.platform.build(self, *args, **kwargs)

# LiteXSoC -----------------------------------------------------------------------------------------

class LiteXSoC(SoC):
    # Add Identifier -------------------------------------------------------------------------------
    def add_identifier(self, name="identifier", identifier="LiteX SoC", with_build_time=True):
        from litex.soc.cores.identifier import Identifier
        self.check_if_exists(name)
        if with_build_time:
            identifier += " " + build_time()
            self.add_config("WITH_BUILD_TIME")
        setattr(self.submodules, name, Identifier(identifier))

    # Add UART -------------------------------------------------------------------------------------
    def add_uart(self, name, baudrate=115200, fifo_depth=16):
        from litex.soc.cores import uart
        self.check_if_exists("uart")

        # Stub / Stream.
        if name in ["stub", "stream"]:
            self.submodules.uart = uart.UART(tx_fifo_depth=0, rx_fifo_depth=0)
            if name == "stub":
                self.comb += self.uart.sink.ready.eq(1)

        # UARTBone / Bridge.
        elif name in ["uartbone", "bridge"]:
            self.add_uartbone(baudrate=baudrate)

        # Crossover.
        elif name in ["crossover"]:
            self.submodules.uart = uart.UARTCrossover(
                tx_fifo_depth = fifo_depth,
                rx_fifo_depth = fifo_depth)

        # Crossover + Bridge.
        elif name in ["crossover+bridge"]:
            self.add_uartbone(baudrate=baudrate)
            self.submodules.uart = uart.UARTCrossover(
                tx_fifo_depth = fifo_depth,
                rx_fifo_depth = fifo_depth)

        # Model/Sim.
        elif name in ["model", "sim"]:
            self.submodules.uart_phy = uart.RS232PHYModel(self.platform.request("serial"))
            self.submodules.uart = uart.UART(self.uart_phy,
                tx_fifo_depth = fifo_depth,
                rx_fifo_depth = fifo_depth)

        # JTAG Atlantic.
        elif name in ["jtag_atlantic"]:
            from litex.soc.cores.jtag import JTAGAtlantic
            self.submodules.uart_phy = JTAGAtlantic()
            self.submodules.uart = uart.UART(self.uart_phy,
                tx_fifo_depth = fifo_depth,
                rx_fifo_depth = fifo_depth)

        # JTAG UART.
        elif name in ["jtag_uart"]:
            from litex.soc.cores.jtag import JTAGPHY
            self.clock_domains.cd_sys_jtag = ClockDomain()          # Run JTAG-UART in sys_jtag clock domain similar to
            self.comb += self.cd_sys_jtag.clk.eq(ClockSignal("sys")) # sys clock domain but with rst disconnected.
            self.submodules.uart_phy = JTAGPHY(device=self.platform.device, clock_domain="sys_jtag")
            self.submodules.uart = uart.UART(self.uart_phy,
                tx_fifo_depth = fifo_depth,
                rx_fifo_depth = fifo_depth)

        # USB ACM (with ValentyUSB core).
        elif name in ["usb_acm"]:
            import valentyusb.usbcore.io as usbio
            import valentyusb.usbcore.cpu.cdc_eptri as cdc_eptri
            usb_pads = self.platform.request("usb")
            usb_iobuf = usbio.IoBuf(usb_pads.d_p, usb_pads.d_n, usb_pads.pullup)
            self.clock_domains.cd_sys_usb = ClockDomain()           # Run USB ACM in sys_usb clock domain similar to
            self.comb += self.cd_sys_usb.clk.eq(ClockSignal("sys")) # sys clock domain but with rst disconnected.
            self.submodules.uart = ClockDomainsRenamer("sys_usb")(cdc_eptri.CDCUsb(usb_iobuf))

        # Classical UART.
        else:
            self.submodules.uart_phy = uart.UARTPHY(
                pads     = self.platform.request(name),
                clk_freq = self.sys_clk_freq,
                baudrate = baudrate)
            self.submodules.uart = uart.UART(self.uart_phy,
                tx_fifo_depth = fifo_depth,
                rx_fifo_depth = fifo_depth)

        if self.irq.enabled:
            self.irq.add("uart", use_loc_if_exists=True)
        else:
            self.add_constant("UART_POLLING")

    # Add UARTbone ---------------------------------------------------------------------------------
    def add_uartbone(self, name="serial", clk_freq=None, baudrate=115200, cd="sys"):
        from litex.soc.cores import uart
        if clk_freq is None:
            clk_freq = self.sys_clk_freq
        self.check_if_exists("uartbone")
        self.submodules.uartbone_phy = uart.UARTPHY(self.platform.request(name), clk_freq, baudrate)
        self.submodules.uartbone = uart.UARTBone(phy=self.uartbone_phy, clk_freq=clk_freq, cd=cd)
        self.bus.add_master(name="uartbone", master=self.uartbone.wishbone)

    # Add JTAGbone ---------------------------------------------------------------------------------
    def add_jtagbone(self, chain=1):
        from litex.soc.cores import uart
        from litex.soc.cores.jtag import JTAGPHY
        self.check_if_exists("jtabone")
        self.submodules.jtagbone_phy = JTAGPHY(device=self.platform.device, chain=chain)
        self.submodules.jtagbone = uart.UARTBone(phy=self.jtagbone_phy, clk_freq=self.sys_clk_freq)
        self.bus.add_master(name="jtagbone", master=self.jtagbone.wishbone)

    # Add SDRAM ------------------------------------------------------------------------------------
    def add_sdram(self, name, phy, module, origin=None, size=None, with_bist=False, with_soc_interconnect=True,
        l2_cache_size           = 8192,
        l2_cache_min_data_width = 128,
        l2_cache_reverse        = True,
        l2_cache_full_memory_we = True,
        **kwargs):

        # Imports.
        from litedram.common import LiteDRAMNativePort
        from litedram.core import LiteDRAMCore
        from litedram.frontend.wishbone import LiteDRAMWishbone2Native
        from litedram.frontend.axi import LiteDRAMAXI2Native
        from litedram.frontend.bist import  LiteDRAMBISTGenerator, LiteDRAMBISTChecker

        # LiteDRAM core.
        self.check_if_exists("sdram")
        self.submodules.sdram = LiteDRAMCore(
            phy             = phy,
            geom_settings   = module.geom_settings,
            timing_settings = module.timing_settings,
            clk_freq        = self.sys_clk_freq,
            **kwargs)

        # Save SPD data to be able to verify it at runtime.
        if hasattr(module, "_spd_data"):
            # Pack the data into words of bus width.
            bytes_per_word = self.bus.data_width // 8
            mem = [0] * ceil(len(module._spd_data) / bytes_per_word)
            for i in range(len(mem)):
                for offset in range(bytes_per_word):
                    mem[i] <<= 8
                    if self.cpu.endianness == "little":
                        offset = bytes_per_word - 1 - offset
                    spd_byte = i * bytes_per_word + offset
                    if spd_byte < len(module._spd_data):
                        mem[i] |= module._spd_data[spd_byte]
            self.add_rom(
                name     = "spd",
                origin   = self.mem_map.get("spd", None),
                size     = len(module._spd_data),
                contents = mem,
            )

        # LiteDRAM BIST.
        if with_bist:
            self.submodules.sdram_generator = LiteDRAMBISTGenerator(self.sdram.crossbar.get_port())
            self.submodules.sdram_checker = LiteDRAMBISTChecker(self.sdram.crossbar.get_port())

        if not with_soc_interconnect: return

        # Compute/Check SDRAM size.
        sdram_size = 2**(module.geom_settings.bankbits +
                         module.geom_settings.rowbits +
                         module.geom_settings.colbits)*phy.settings.nranks*phy.settings.databits//8
        if size is not None:
            sdram_size = min(sdram_size, size)

        # Add SDRAM region.
        self.bus.add_region("main_ram", SoCRegion(origin=self.mem_map.get("main_ram", origin), size=sdram_size))

        # Add CPU's direct memory buses (if not already declared) ----------------------------------
        if hasattr(self.cpu, "add_memory_buses"):
            self.cpu.add_memory_buses(
                address_width = 32,
                data_width    = self.sdram.crossbar.controller.data_width
            )

        # Connect CPU's direct memory buses to LiteDRAM --------------------------------------------
        if len(self.cpu.memory_buses):
            # When CPU has at least a direct memory bus, connect them directly to LiteDRAM.
            for mem_bus in self.cpu.memory_buses:
                # Request a LiteDRAM native port.
                port = self.sdram.crossbar.get_port()
                port.data_width = 2**int(log2(port.data_width)) # Round to nearest power of 2.

                # Check if bus is an AXI bus and connect it.
                if isinstance(mem_bus, axi.AXIInterface):
                    # If same data_width, connect it directly.
                    if port.data_width == mem_bus.data_width:
                        self.logger.info("Matching AXI MEM data width ({})\n".format(port.data_width))
                        self.submodules += LiteDRAMAXI2Native(
                            axi          = self.cpu.mem_axi,
                            port         = port,
                            base_address = self.bus.regions["main_ram"].origin)
                    # If different data_width, do the adaptation and connect it via Wishbone.
                    else:
                        self.logger.info("Converting MEM data width: {} to {} via Wishbone".format(
                            port.data_width,
                            self.cpu.mem_axi.data_width))
                        # FIXME: Replace WB data-width converter with native AXI converter.
                        mem_wb  = wishbone.Interface(
                            data_width = self.cpu.mem_axi.data_width,
                            adr_width  = 32-log2_int(self.cpu.mem_axi.data_width//8))
                        mem_a2w = axi.AXI2Wishbone(
                            axi          = self.cpu.mem_axi,
                            wishbone     = mem_wb,
                            base_address = 0)
                        self.submodules += mem_a2w
                        litedram_wb = wishbone.Interface(port.data_width)
                        self.submodules += LiteDRAMWishbone2Native(
                            wishbone     = litedram_wb,
                            port         = port,
                            base_address = self.bus.regions["main_ram"].origin)
                        self.submodules += wishbone.Converter(mem_wb, litedram_wb)
                # Check if bus is a Native bus and connect it.
                if isinstance(mem_bus, LiteDRAMNativePort):
                    # If same data_width, connect it directly.
                    if port.data_width == mem_bus.data_width:
                        self.comb += mem_bus.cmd.connect(port.cmd)
                        self.comb += mem_bus.wdata.connect(port.wdata)
                        self.comb += port.rdata.connect(mem_bus.rdata)
                    # Else raise Error.
                    else:
                        raise NotImplementedError

        # Connect Main bus to LiteDRAM (with optional L2 Cache) ------------------------------------
        connect_main_bus_to_dram = (
            # No memory buses.
            (not len(self.cpu.memory_buses)) or
            # Memory buses but no DMA bus.
            (len(self.cpu.memory_buses) and not hasattr(self.cpu, "dma_bus"))
        )
        if connect_main_bus_to_dram:
            # Request a LiteDRAM native port.
            port = self.sdram.crossbar.get_port()
            port.data_width = 2**int(log2(port.data_width)) # Round to nearest power of 2.

            # Create Wishbone Slave.
            wb_sdram = wishbone.Interface()
            self.bus.add_slave("main_ram", wb_sdram)

            # L2 Cache
            if l2_cache_size != 0:
                # Insert L2 cache inbetween Wishbone bus and LiteDRAM
                l2_cache_size = max(l2_cache_size, int(2*port.data_width/8)) # Use minimal size if lower
                l2_cache_size = 2**int(log2(l2_cache_size))                  # Round to nearest power of 2
                l2_cache_data_width = max(port.data_width, l2_cache_min_data_width)
                l2_cache = wishbone.Cache(
                    cachesize = l2_cache_size//4,
                    master    = wb_sdram,
                    slave     = wishbone.Interface(l2_cache_data_width),
                    reverse   = l2_cache_reverse)
                if l2_cache_full_memory_we:
                    l2_cache = FullMemoryWE()(l2_cache)
                self.submodules.l2_cache = l2_cache
                litedram_wb = self.l2_cache.slave
            else:
                litedram_wb = wishbone.Interface(port.data_width)
                self.submodules += wishbone.Converter(wb_sdram, litedram_wb)
            self.add_config("L2_SIZE", l2_cache_size)

            # Wishbone Slave <--> LiteDRAM bridge.
            self.submodules.wishbone_bridge = LiteDRAMWishbone2Native(
                wishbone     = litedram_wb,
                port         = port,
                base_address = self.bus.regions["main_ram"].origin)

    # Add Ethernet ---------------------------------------------------------------------------------
    def add_ethernet(self, name="ethmac", phy=None, phy_cd="eth", dynamic_ip=False, software_debug=False,
        nrxslots                = 2,
        ntxslots                = 2,
        with_timestamp          = False,
        with_timing_constraints = True):
        # Imports
        from liteeth.mac import LiteEthMAC
        from liteeth.phy.model import LiteEthPHYModel

        # MAC.
        self.check_if_exists(name)
        if with_timestamp:
            self.timer0.add_uptime()
        ethmac = LiteEthMAC(
            phy        = phy,
            dw         = 32,
            interface  = "wishbone",
            endianness = self.cpu.endianness,
            nrxslots   = nrxslots,
            ntxslots   = ntxslots,
            timestamp  = None if not with_timestamp else self.timer0.uptime_cycles,
            with_preamble_crc = not software_debug)
        # Use PHY's eth_tx/eth_rx clock domains.
        ethmac = ClockDomainsRenamer({
            "eth_tx": phy_cd + "_tx",
            "eth_rx": phy_cd + "_rx"})(ethmac)
        setattr(self.submodules, name, ethmac)
        # Compute Regions size and add it to the SoC.
        ethmac_region_size = (ethmac.rx_slots.read() + ethmac.tx_slots.read())*ethmac.slot_size.read()
        ethmac_region = SoCRegion(origin=self.mem_map.get(name, None), size=ethmac_region_size, cached=False)
        self.bus.add_slave(name=name, slave=ethmac.bus, region=ethmac_region)
        # Add IRQs (if enabled).
        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

        # Dynamic IP (if enabled).
        if dynamic_ip:
            self.add_constant("ETH_DYNAMIC_IP")

        # Software Debug
        if software_debug:
            self.add_constant("ETH_UDP_TX_DEBUG")
            self.add_constant("ETH_UDP_RX_DEBUG")

        # Timing constraints
        if with_timing_constraints:
            eth_rx_clk = getattr(phy, "crg", phy).cd_eth_rx.clk
            eth_tx_clk = getattr(phy, "crg", phy).cd_eth_tx.clk
            if not isinstance(phy, LiteEthPHYModel) and not getattr(phy, "model", False):
                self.platform.add_period_constraint(eth_rx_clk, 1e9/phy.rx_clk_freq)
                self.platform.add_period_constraint(eth_tx_clk, 1e9/phy.tx_clk_freq)
                self.platform.add_false_path_constraints(self.crg.cd_sys.clk, eth_rx_clk, eth_tx_clk)

    # Add Etherbone --------------------------------------------------------------------------------
    def add_etherbone(self, name="etherbone", phy=None, phy_cd="eth",
        mac_address             = 0x10e2d5000000,
        ip_address              = "192.168.1.50",
        udp_port                = 1234,
        buffer_depth            = 4,
        with_timing_constraints = True):
        # Imports
        from liteeth.core import LiteEthUDPIPCore
        from liteeth.frontend.etherbone import LiteEthEtherbone
        from liteeth.phy.model import LiteEthPHYModel

        # Core
        self.check_if_exists(name)
        ethcore = LiteEthUDPIPCore(
            phy         = phy,
            mac_address = mac_address,
            ip_address  = ip_address,
            clk_freq    = self.clk_freq)
        # Use PHY's eth_tx/eth_rx clock domains.
        ethcore = ClockDomainsRenamer({
            "eth_tx": phy_cd + "_tx",
            "eth_rx": phy_cd + "_rx",
            "sys":    phy_cd + "_rx"})(ethcore)
        self.submodules.ethcore = ethcore

        # Create Etherbone clock domain and run it from sys clock domain.
        self.clock_domains.cd_etherbone = ClockDomain("etherbone")
        self.comb += self.cd_etherbone.clk.eq(ClockSignal("sys"))
        self.comb += self.cd_etherbone.rst.eq(ResetSignal("sys"))

        # Etherbone
        etherbone = LiteEthEtherbone(ethcore.udp, udp_port, buffer_depth=buffer_depth, cd="etherbone")
        setattr(self.submodules, name, etherbone)
        self.add_wb_master(etherbone.wishbone.bus)

        # Timing constraints
        if with_timing_constraints:
            eth_rx_clk = getattr(phy, "crg", phy).cd_eth_rx.clk
            eth_tx_clk = getattr(phy, "crg", phy).cd_eth_tx.clk
            if not isinstance(phy, LiteEthPHYModel) and not getattr(phy, "model", False):
                self.platform.add_period_constraint(eth_rx_clk, 1e9/phy.rx_clk_freq)
                self.platform.add_period_constraint(eth_tx_clk, 1e9/phy.tx_clk_freq)
                self.platform.add_false_path_constraints(self.crg.cd_sys.clk, eth_rx_clk, eth_tx_clk)

    # Add SPI Flash --------------------------------------------------------------------------------
    def add_spi_flash(self, name="spiflash", mode="4x", dummy_cycles=None, clk_freq=None, module=None, phy=None, rate="1:1", **kwargs):
        if module is None:
            # Use previous LiteX SPI Flash core with compat, will be deprecated at some point.
            from litex.compat.soc_add_spi_flash import add_spi_flash
            add_spi_flash(self, name, mode, dummy_cycles)
        # LiteSPI.
        else:
            # Imports.
            from litespi import LiteSPI
            from litespi.phy.generic import LiteSPIPHY
            from litespi.opcodes import SpiNorFlashOpCodes

            # Checks/Parameters.
            assert mode in ["1x", "4x"]
            if clk_freq is None: clk_freq = self.sys_clk_freq

            # PHY.
            spiflash_phy = phy
            if spiflash_phy is None:
                self.check_if_exists(name + "_phy")
                spiflash_pads = self.platform.request(name if mode == "1x" else name + mode)
                spiflash_phy = LiteSPIPHY(spiflash_pads, module, device=self.platform.device, default_divisor=int(self.sys_clk_freq/clk_freq), rate=rate)
                setattr(self.submodules, name + "_phy",  spiflash_phy)

            # Core.
            self.check_if_exists(name + "_mmap")
            spiflash_core = LiteSPI(spiflash_phy, mmap_endianness=self.cpu.endianness, **kwargs)
            setattr(self.submodules, name + "_core", spiflash_core)
            spiflash_region = SoCRegion(origin=self.mem_map.get(name, None), size=module.total_size)
            self.bus.add_slave(name=name, slave=spiflash_core.bus, region=spiflash_region)

            # Constants.
            self.add_constant("SPIFLASH_PHY_FREQUENCY", clk_freq)
            self.add_constant("SPIFLASH_MODULE_NAME", module.name.upper())
            self.add_constant("SPIFLASH_MODULE_TOTAL_SIZE", module.total_size)
            self.add_constant("SPIFLASH_MODULE_PAGE_SIZE", module.page_size)
            if SpiNorFlashOpCodes.READ_1_1_4 in module.supported_opcodes:
                self.add_constant("SPIFLASH_MODULE_QUAD_CAPABLE")
            if SpiNorFlashOpCodes.READ_4_4_4 in module.supported_opcodes:
                self.add_constant("SPIFLASH_MODULE_QPI_CAPABLE")

    # Add SPI SDCard -------------------------------------------------------------------------------
    def add_spi_sdcard(self, name="spisdcard", spi_clk_freq=400e3, software_debug=False):
        # Imports.
        from litex.soc.cores.spi import SPIMaster

        # Pads.
        pads = self.platform.request(name)
        if hasattr(pads, "rst"):
            self.comb += pads.rst.eq(0)

        # Core.
        self.check_if_exists(name)
        spisdcard = SPIMaster(pads, 8, self.sys_clk_freq, spi_clk_freq)
        spisdcard.add_clk_divider()
        setattr(self.submodules, name, spisdcard)

        # Debug.
        if software_debug:
            self.add_constant("SPISDCARD_DEBUG")

    # Add SDCard -----------------------------------------------------------------------------------
    def add_sdcard(self, name="sdcard", mode="read+write", use_emulator=False, software_debug=False):
        # Imports.
        from litesdcard.emulator import SDEmulator
        from litesdcard.phy import SDPHY
        from litesdcard.core import SDCore
        from litesdcard.frontend.dma import SDBlock2MemDMA, SDMem2BlockDMA

        # Checks.
        assert mode in ["read", "write", "read+write"]

        # Emulator / Pads.
        if use_emulator:
            sdemulator = SDEmulator(self.platform)
            self.submodules += sdemulator
            sdcard_pads = sdemulator.pads
        else:
            sdcard_pads = self.platform.request(name)

        # Core.
        self.check_if_exists("sdphy")
        self.check_if_exists("sdcore")
        self.submodules.sdphy  = SDPHY(sdcard_pads, self.platform.device, self.clk_freq, cmd_timeout=10e-1, data_timeout=10e-1)
        self.submodules.sdcore = SDCore(self.sdphy)

        # Block2Mem DMA.
        if "read" in mode:
            bus = wishbone.Interface(data_width=self.bus.data_width, adr_width=self.bus.address_width)
            self.submodules.sdblock2mem = SDBlock2MemDMA(bus=bus, endianness=self.cpu.endianness)
            self.comb += self.sdcore.source.connect(self.sdblock2mem.sink)
            dma_bus = self.bus if not hasattr(self, "dma_bus") else self.dma_bus
            dma_bus.add_master("sdblock2mem", master=bus)

        # Mem2Block DMA.
        if "write" in mode:
            bus = wishbone.Interface(data_width=self.bus.data_width, adr_width=self.bus.address_width)
            self.submodules.sdmem2block = SDMem2BlockDMA(bus=bus, endianness=self.cpu.endianness)
            self.comb += self.sdmem2block.source.connect(self.sdcore.sink)
            dma_bus = self.bus if not hasattr(self, "dma_bus") else self.dma_bus
            dma_bus.add_master("sdmem2block", master=bus)

        # Interrupts.
        self.submodules.sdirq = EventManager()
        self.sdirq.card_detect   = EventSourcePulse(description="SDCard has been ejected/inserted.")
        if "read" in mode:
            self.sdirq.block2mem_dma = EventSourcePulse(description="Block2Mem DMA terminated.")
        if "write" in mode:
            self.sdirq.mem2block_dma = EventSourcePulse(description="Mem2Block DMA terminated.")
        self.sdirq.cmd_done      = EventSourceLevel(description="Command completed.")
        self.sdirq.finalize()
        if "read" in mode:
            self.comb += self.sdirq.block2mem_dma.trigger.eq(self.sdblock2mem.irq)
        if "write" in mode:
            self.comb += self.sdirq.mem2block_dma.trigger.eq(self.sdmem2block.irq)
        self.comb += [
            self.sdirq.card_detect.trigger.eq(self.sdphy.card_detect_irq),
            self.sdirq.cmd_done.trigger.eq(self.sdcore.cmd_event.fields.done)
        ]
        if self.irq.enabled:
            self.irq.add("sdirq", use_loc_if_exists=True)

        # Debug.
        if software_debug:
            self.add_constant("SDCARD_DEBUG")

    # Add SATA -------------------------------------------------------------------------------------
    def add_sata(self, name="sata", phy=None, mode="read+write"):
        # Imports.
        from litesata.core import LiteSATACore
        from litesata.frontend.arbitration import LiteSATACrossbar
        from litesata.frontend.dma import LiteSATASector2MemDMA, LiteSATAMem2SectorDMA

        # Checks.
        assert mode in ["read", "write", "read+write"]
        sata_clk_freqs = {
            "gen1":  75e6,
            "gen2": 150e6,
            "gen3": 300e6,
        }
        sata_clk_freq = sata_clk_freqs[phy.gen]
        assert self.clk_freq >= sata_clk_freq/2 # FIXME: /2 for 16-bit data-width, add support for 32-bit.

        # Core.
        self.check_if_exists("sata_core")
        self.submodules.sata_core = LiteSATACore(phy)

        # Crossbar.
        self.check_if_exists("sata_crossbar")
        self.submodules.sata_crossbar = LiteSATACrossbar(self.sata_core)

        # Sector2Mem DMA.
        if "read" in mode:
            bus = wishbone.Interface(data_width=self.bus.data_width, adr_width=self.bus.address_width)
            self.submodules.sata_sector2mem = LiteSATASector2MemDMA(
               port       = self.sata_crossbar.get_port(),
               bus        = bus,
               endianness = self.cpu.endianness)
            dma_bus = self.bus if not hasattr(self, "dma_bus") else self.dma_bus
            dma_bus.add_master("sata_sector2mem", master=bus)

        # Mem2Sector DMA.
        if "write" in mode:
            bus = wishbone.Interface(data_width=self.bus.data_width, adr_width=self.bus.address_width)
            self.submodules.sata_mem2sector = LiteSATAMem2SectorDMA(
               bus        = bus,
               port       = self.sata_crossbar.get_port(),
               endianness = self.cpu.endianness)
            dma_bus = self.bus if not hasattr(self, "dma_bus") else self.dma_bus
            dma_bus.add_master("sata_mem2sector", master=bus)

        # Timing constraints.
        self.platform.add_period_constraint(self.sata_phy.crg.cd_sata_tx.clk, 1e9/sata_clk_freq)
        self.platform.add_period_constraint(self.sata_phy.crg.cd_sata_rx.clk, 1e9/sata_clk_freq)
        self.platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            self.sata_phy.crg.cd_sata_tx.clk,
            self.sata_phy.crg.cd_sata_rx.clk)

    # Add PCIe -------------------------------------------------------------------------------------
    def add_pcie(self, name="pcie", phy=None, ndmas=0, max_pending_requests=8,
        with_dma_buffering = True, dma_buffering_depth=1024,
        with_dma_loopback  = True,
        with_msi           = True):
        # Imports
        from litepcie.core import LitePCIeEndpoint, LitePCIeMSI
        from litepcie.frontend.dma import LitePCIeDMA
        from litepcie.frontend.wishbone import LitePCIeWishboneMaster

        # Checks.
        assert self.csr.data_width == 32

        # Endpoint.
        self.check_if_exists(f"{name}_endpoint")
        endpoint = LitePCIeEndpoint(phy, max_pending_requests=max_pending_requests, endianness=phy.endianness)
        setattr(self.submodules, f"{name}_endpoint", endpoint)

        # MMAP.
        self.check_if_exists(f"{name}_mmap")
        mmap = LitePCIeWishboneMaster(self.pcie_endpoint, base_address=self.mem_map["csr"])
        self.add_wb_master(mmap.wishbone)
        setattr(self.submodules, f"{name}_mmap", mmap)

        # MSI.
        if with_msi:
            self.check_if_exists(f"{name}_msi")
            msi = LitePCIeMSI()
            setattr(self.submodules, f"{name}_msi", msi)
            self.comb += msi.source.connect(phy.msi)
            self.msis = {}

        # DMAs.
        for i in range(ndmas):
            assert with_msi
            self.check_if_exists(f"{name}_dma{i}")
            dma = LitePCIeDMA(phy, endpoint,
                with_buffering = with_dma_buffering, buffering_depth=dma_buffering_depth,
                with_loopback  = with_dma_loopback)
            setattr(self.submodules, f"{name}_dma{i}", dma)
            self.msis[f"{name.upper()}_DMA{i}_WRITER"] = dma.writer.irq
            self.msis[f"{name.upper()}_DMA{i}_READER"] = dma.reader.irq
        self.add_constant("DMA_CHANNELS", ndmas)

        # Map/Connect IRQs.
        if with_msi:
            for i, (k, v) in enumerate(sorted(self.msis.items())):
                self.comb += msi.irqs[i].eq(v)
                self.add_constant(k + "_INTERRUPT", i)

        # Timing constraints.
        self.platform.add_false_path_constraints(self.crg.cd_sys.clk, phy.cd_pcie.clk)

    # Add Video ColorBars Pattern ------------------------------------------------------------------
    def add_video_colorbars(self, name="video_colorbars", phy=None, timings="800x600@60Hz", clock_domain="sys"):
        # Imports.
        from litex.soc.cores.video import VideoTimingGenerator, ColorBarsPattern

        # Video Timing Generator.
        self.check_if_exists(f"{name}_vtg")
        vtg = VideoTimingGenerator(default_video_timings=timings if isinstance(timings, str) else timings[1])
        vtg = ClockDomainsRenamer(clock_domain)(vtg)
        setattr(self.submodules, f"{name}_vtg", vtg)

        # ColorsBars Pattern.
        self.check_if_exists(name)
        colorbars = ClockDomainsRenamer(clock_domain)(ColorBarsPattern())
        setattr(self.submodules, name, colorbars)

        # Connect Video Timing Generator to ColorsBars Pattern.
        self.comb += [
            vtg.source.connect(colorbars.vtg_sink),
            colorbars.source.connect(phy if isinstance(phy, stream.Endpoint) else phy.sink)
        ]

    # Add Video Terminal ---------------------------------------------------------------------------
    def add_video_terminal(self, name="video_terminal", phy=None, timings="800x600@60Hz", clock_domain="sys"):
        # Imports.
        from litex.soc.cores.video import VideoTimingGenerator, VideoTerminal

        # Video Timing Generator.
        self.check_if_exists(f"{name}_vtg")
        vtg = VideoTimingGenerator(default_video_timings=timings if isinstance(timings, str) else timings[1])
        vtg = ClockDomainsRenamer(clock_domain)(vtg)
        setattr(self.submodules, f"{name}_vtg", vtg)

        # Video Terminal.
        timings = timings if isinstance(timings, str) else timings[0]
        vt = VideoTerminal(
            hres = int(timings.split("@")[0].split("x")[0]),
            vres = int(timings.split("@")[0].split("x")[1]),
        )
        vt = ClockDomainsRenamer(clock_domain)(vt)
        setattr(self.submodules, name, vt)

        # Connect Video Timing Generator to Video Terminal.
        self.comb += vtg.source.connect(vt.vtg_sink)

        # Connect UART to Video Terminal.
        uart_cdc = stream.ClockDomainCrossing([("data", 8)], cd_from="sys", cd_to=clock_domain)
        setattr(self.submodules, f"{name}_uart_cdc", uart_cdc)
        self.comb += [
            uart_cdc.sink.valid.eq(self.uart.tx_fifo.source.valid & self.uart.tx_fifo.source.ready),
            uart_cdc.sink.data.eq(self.uart.tx_fifo.source.data),
            uart_cdc.source.connect(vt.uart_sink),
        ]

        # Connect Video Terminal to Video PHY.
        self.comb += vt.source.connect(phy if isinstance(phy, stream.Endpoint) else phy.sink)

    # Add Video Framebuffer ------------------------------------------------------------------------
    def add_video_framebuffer(self, name="video_framebuffer", phy=None, timings="800x600@60Hz", clock_domain="sys", format="rgb888"):
        # Imports.
        from litex.soc.cores.video import VideoTimingGenerator, VideoFrameBuffer

        # Video Timing Generator.
        vtg = VideoTimingGenerator(default_video_timings=timings if isinstance(timings, str) else timings[1])
        vtg = ClockDomainsRenamer(clock_domain)(vtg)
        setattr(self.submodules, f"{name}_vtg", vtg)

        # Video FrameBuffer.
        timings = timings if isinstance(timings, str) else timings[0]
        base = self.mem_map.get(name, 0x40c00000)
        hres = int(timings.split("@")[0].split("x")[0])
        vres = int(timings.split("@")[0].split("x")[1])
        vfb = VideoFrameBuffer(self.sdram.crossbar.get_port(),
            hres   = hres,
            vres   = vres,
            base   = base,
            format = format,
            clock_domain          = clock_domain,
            clock_faster_than_sys = vtg.video_timings["pix_clk"] > self.sys_clk_freq,
        )
        setattr(self.submodules, name, vfb)

        # Connect Video Timing Generator to Video FrameBuffer.
        self.comb += vtg.source.connect(vfb.vtg_sink)

        # Connect Video FrameBuffer to Video PHY.
        self.comb += vfb.source.connect(phy if isinstance(phy, stream.Endpoint) else phy.sink)

        # Constants.
        self.add_constant("VIDEO_FRAMEBUFFER_BASE", base)
        self.add_constant("VIDEO_FRAMEBUFFER_HRES", hres)
        self.add_constant("VIDEO_FRAMEBUFFER_VRES", vres)
        self.add_constant("VIDEO_FRAMEBUFFER_DEPTH", vfb.depth)

