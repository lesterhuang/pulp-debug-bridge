#
# Copyright (C) 2018 ETH Zurich and University of Bologna
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
#

# Authors: Germain Haugou, ETH (germain.haugou@iis.ee.ethz.ch)

import ctypes
import os
import os.path
import json_tools as js
from elftools.elf.elffile import ELFFile
import time
from portable import to_bytes
import struct

class DebugBridgeException(Exception):
    pass

class XferInvalidObjectException(DebugBridgeException):
    pass

class XferInvalidAnnexException(DebugBridgeException):
    pass

class XferErrorException(DebugBridgeException):
    
    def __init__(self, error_code):
        self.error_code = error_code

    def __str__(self):
        return "E {0:02X}".format(self.error_code)

class Ctype_cable(object):

    def __init__(self, module, config, system_config):

        self.module = module
        self.gdb_handle = None

        # Register entry points with appropriate arguments
        self.module.cable_new.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self.module.cable_new.restype = ctypes.c_void_p

        self.module.cable_write.argtypes = \
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]

        self.module.cable_read.argtypes = \
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]

        self.module.chip_reset.argtypes = \
            [ctypes.c_void_p, ctypes.c_bool]
            
        self.module.jtag_reset.argtypes = \
            [ctypes.c_void_p, ctypes.c_bool]
            
        self.module.jtag_soft_reset.argtypes = \
            [ctypes.c_void_p]
            
        self.module.cable_jtag_set_reg.argtypes = \
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        self.module.cable_jtag_set_reg.restype = ctypes.c_bool

        self.module.cable_jtag_get_reg.argtypes = \
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.module.cable_jtag_get_reg.restype = ctypes.c_bool

        self.module.cable_lock.argtypes = \
            [ctypes.c_void_p]
            
        self.module.cable_unlock.argtypes = \
            [ctypes.c_void_p]
            
        self.module.bridge_get_error.argtypes = []
        self.module.bridge_get_error.restype = ctypes.c_char_p
            
        self.module.bridge_init.argtypes = [ctypes.c_char_p, ctypes.c_int]

        self.cmd_func_typ = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char), ctypes.c_int)
        self.module.gdb_server_open.argtypes = [ctypes.c_void_p, ctypes.c_int, self.cmd_func_typ, ctypes.c_char_p]
        self.module.gdb_server_open.restype = ctypes.c_void_p

        self.module.gdb_server_close.argtypes = [ctypes.c_void_p, ctypes.c_int]

        self.module.gdb_server_refresh_target.argtypes = [ctypes.c_void_p]

        config_string = None

        if config is not None:
            config_string = config.dump_to_string().encode('utf-8')

        self.instance = self.module.cable_new(config_string, system_config.dump_to_string().encode('utf-8'))

        if self.instance == None:
            raise Exception('Failed to initialize cable with error: ' + self.module.bridge_get_error().decode('utf-8'))
        

    def get_instance(self):
        return self.instance

    def write(self, addr, size, buffer):
        data = (ctypes.c_char * size).from_buffer(bytearray(buffer))
        self.module.cable_write(self.instance, addr, size, data)

    def read(self, addr, size):
        data = (ctypes.c_char * size)()
        self.module.cable_read(self.instance, addr, size, data)

        result = []
        for elem in data:
            result.append(elem)

        return result

    def chip_reset(self, value):
        self.module.chip_reset(self.instance, value)

    def jtag_reset(self, value):
        self.module.jtag_reset(self.instance, value)

    def jtag_soft_reset(self):
        self.module.jtag_soft_reset(self.instance)

    def jtag_set_reg(self, reg, width, value):
        self.module.cable_jtag_set_reg(self.instance, reg, width, value)

    def jtag_get_reg(self, reg, width, value):
        out_value = ctypes.c_int()
        self.module.cable_jtag_get_reg(self.instance, reg, width, ctypes.byref(out_value), value)
        return out_value.value

    def lock(self):
        self.module.cable_lock(self.instance)

    def unlock(self):
        self.module.cable_unlock(self.instance)




class debug_bridge(object):

    def __init__(self, config, binaries=[], verbose=0):
        print("Verbose", verbose)
        self.config = config
        self.cable = None
        self.cable_name = config.get('**/debug_bridge/cable/type').get()
        self.binaries = binaries
        self.ioloop_handle = None
        self.reqloop_handle = None
        self.verbose = verbose
        self.gdb_handle = None
        self.cable_config = config.get('**/debug_bridge/cable')
        self.is_started = None

        # Load the library which provides generic services through
        # python / C++ bindings
        lib_path=os.path.join('libpulpdebugbridge.so')
        self.module = ctypes.CDLL(lib_path)

        self.module.bridge_ioloop_open.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.module.bridge_ioloop_open.restype = ctypes.c_void_p

        self.module.bridge_ioloop_close.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.module.bridge_ioloop_close.restype = ctypes.c_int

        self.module.bridge_reqloop_open.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.module.bridge_reqloop_open.restype = ctypes.c_void_p
        
        self.module.bridge_reqloop_close.argtypes = [ctypes.c_void_p, ctypes.c_int]

        self.module.bridge_init(config.dump_to_string().encode('utf-8'), verbose)

        self.capabilities("")

        #self.module.jtag_shift.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)]

    def __mount_cable(self):
        if self.cable_name is None:
            raise Exception("Trying to mount cable while no cable was specified")

        if self.cable_name.split('@')[0] in ['ftdi', 'jtag-proxy']:
            self.__mount_ctype_cable()
            pass
        else:
            raise Exception('Unknown cable: ' + self.cable_name)

    def __mount_ctype_cable(self):

        self.cable = Ctype_cable(
            module = self.module,
            config = self.cable_config,
            system_config = self.config
        )

    def get_cable(self):
        if self.cable is None:
            self.__mount_cable()

        return self.cable

    def load_jtag(self):
        raise Exception('JTAG boot is not supported on this target')

    def load_jtag_hyper(self):
        raise Exception('JTAG boot is not supported on this target')

    def load_elf(self, binary):
        with open(binary, 'rb') as file:
            elffile = ELFFile(file)

            for segment in elffile.iter_segments():

                if segment['p_type'] == 'PT_LOAD':

                    data = segment.data()
                    addr = segment['p_paddr']
                    size = len(data)

                    if self.verbose > 0:
                        print ('Loading section (base: 0x%x, size: 0x%x)' % (addr, size))

                    self.write(addr, size, data)

                    if segment['p_filesz'] < segment['p_memsz']:
                        addr = segment['p_paddr'] + segment['p_filesz']
                        size = segment['p_memsz'] - segment['p_filesz']
                        print ('Init section to 0 (base: 0x%x, size: 0x%x)' % (addr, size))
                        self.write(
                            addr,
                            size,
                            [0] * size
                        )


            set_pc_addr_config = self.config.get('**/debug_bridge/set_pc_addr')
            if set_pc_addr_config is not None:
                return self.write_32(set_pc_addr_config.get_int(), elffile.header['e_entry'])

        return 0

    def load(self):
        mode = self.config.get('**/debug_bridge/boot-mode').get()
        if mode == 'jtag':
            return self.load_jtag()
        elif mode == 'jtag_hyper':
            return self.load_jtag_hyper()
        else:
            return self.load_default()

    def load_default(self):
        for binary in self.binaries:
            if self.load_elf(binary=binary):
                return 1

        return 0

    def start(self):
        start_addr_config = self.config.get('**/debug_bridge/start_addr')
        if start_addr_config is not None:
            self.is_started = True
            self.write_32(start_addr_config.get_int(), self.config.get('**/debug_bridge/start_value').get_int())
        return 0

    def stop(self):
        stop_addr_config = self.config.get('**/debug_bridge/stop_addr')
        if stop_addr_config is not None:
            self.is_started = False
            self.write_32(stop_addr_config.get_int(), self.config.get('**/debug_bridge/stop_value').get_int()) != 0
        return 0

    def read(self, addr, size):
        return self.get_cable().read(addr, size)

    def write(self, addr, size, buffer):
        return self.get_cable().write(addr, size, buffer)

    def write_int(self, addr, value, size):
        return self.write(addr, size, to_bytes(value, size, byteorder='little'))

    def write_32(self, addr, value):
        return self.write_int(addr, value, 4)

    def write_16(self, addr, value):
        return self.write_int(addr, value, 2)

    def write_8(self, addr, value):
        return self.write_int(addr, value, 1)

    def read_int(self, addr, size):
        byte_array = None
        for byte in self.read(addr, size):
            if byte_array == None:
                byte_array = byte
            else:
                byte_array += byte
        return struct.unpack(">i",byte_array)[0]
            # int.from_bytes(byte_array, byteorder='little')

    def read_32(self, addr):
        return self.read_int(addr, 4)

    def read_16(self, addr):
        return self.read_int(addr, 2)

    def read_8(self, addr):
        return self.read_int(addr, 1)

    def _get_binary_symbol_addr(self, name):
        for binary in self.binaries:
            with open(binary, 'rb') as file:
                elf = ELFFile(file)
                for section in elf.iter_sections():
                    if section.header['sh_type'] == 'SHT_SYMTAB':
                        for symbol in section.iter_symbols():
                            if symbol.name == name:
                                t_section=symbol.entry['st_shndx']
                                t_vaddr=symbol.entry['st_value']
                                return t_vaddr
        return 0

    def reset(self):
        self.get_cable().jtag_reset(True)
        self.get_cable().jtag_reset(False)
        self.get_cable().chip_reset(True)
        self.get_cable().chip_reset(False)
        return 0

    def ioloop(self):
        if self.ioloop_handle is not None:
            print("close existing handle ....")
            self.module.bridge_ioloop_close(self.ioloop_handle, 1)

        # First get address of the structure used to communicate between
        # the bridge and the runtime
        addr = self._get_binary_symbol_addr('__rt_debug_struct_ptr')
        print("debug address 0x{:x} contents 0x{:x}".format(addr, self.read_32(addr)))
        
        if addr == 0:
            addr = self._get_binary_symbol_addr('debugStruct_ptr')

        self.ioloop_handle = self.module.bridge_ioloop_open(
            self.get_cable().get_instance(), addr)

        return 0

    def reqloop(self):

        # First get address of the structure used to communicate between
        # the bridge and the runtime
        addr = self._get_binary_symbol_addr('__rt_debug_struct_ptr')
        if addr == 0:
            addr = self._get_binary_symbol_addr('debugStruct_ptr')

        self.reqloop_handle = self.module.bridge_reqloop_open(
            self.get_cable().get_instance(), addr)

        return 0

    def flash(self):
        raise Exception('Flash is not supported on this target')

    def encodeBytes(self, sbuf, buf, buf_len):
        enc = sbuf.encode('ascii')
        if len(enc) + 1 > buf_len:
            return -len(enc)
        for i in range(len(enc)):
            buf[i] = enc[i]
        print(buf, len(enc))
        buf[len(enc)] = b'\000'
        return len(enc)

    def qrcmd_reset(self, cmd, buf, buf_len):
        self.module.bridge_ioloop_close(self.ioloop_handle, 1)
        self.ioloop_handle = None
        self.stop()
        self.load()
        self.module.gdb_server_refresh_target(self.gdb_handle)
        self.ioloop()
        if cmd == "run":
            self.start()
        return self.encodeBytes("OK", buf, buf_len)

    def qrcmd_cb(self, cmd, buf, buf_len):
        cmd = bytearray.fromhex(cmd).decode()
        if cmd.startswith("reset"):
            cmd = cmd.split(' ')
            if len(cmd) == 1:
                cmd = "run"
            elif len(cmd) == 2:
                cmd = cmd[1]
            else:
                return self.encodeBytes("", buf, buf_len)
            if cmd == "run" or cmd == "halt":
                return self.qrcmd_reset(cmd, buf, buf_len)
            else:
                return self.encodeBytes("", buf, buf_len)

    def qxfer_read_cb(self, obj, annex, offset, length, buf, buf_len):
        try:
            if obj == "exec-file":
                if len(self.binaries) > 0:
                    obj_val = os.path.abspath(self.binaries[0])
                else:
                    obj_val = ""
            else:
                obj_val = self.qxfer_read(obj, annex)

            if offset >= len(obj_val):
                obj_val = "l"
            elif offset + length >= len(obj_val):
                obj_val = "l" + obj_val[offset:]
            else:
                obj_val = "m" + obj_val[offset:offset+length-1]

        except XferInvalidObjectException:
            obj_val = ""
        except XferInvalidAnnexException:
            obj_val = "E00"
        except XferErrorException as err:
            obj_val = str(err)
        except Exception:
            obj_val = ""

        return self.encodeBytes(obj_val, buf, buf_len)

    def capabilities(self, extra):
        capstr = "qXfer:exec-file:read+"
        if len(extra) > 0:
            capstr = capstr + ";" + extra
        self.capabilities_str = capstr.encode('ascii')

    def cmd_cb(self, cmd, buf, buf_len):
        try:
            cmd = cmd.decode('ascii')
            if cmd.startswith("qXfer"):
                cmd = cmd.split(":")
                if len(cmd) != 5:
                    raise Exception()
                offlen = cmd[4].split(',')
                if cmd[2] == "read":
                    return self.qxfer_read_cb(cmd[1], cmd[3], int(offlen[0], base=16), int(offlen[1], base=16), buf, buf_len)
            elif cmd.startswith("qRcmd"):
                if len(cmd) < len("qRcmd") + 2:
                    return self.encodeBytes("E01", buf, buf_len)
                cmd = cmd.split(',')
                print(cmd)
                if len(cmd) != 2:
                    return self.encodeBytes("E01", buf, buf_len)

                return self.qrcmd_cb(cmd[1], buf, buf_len)
            elif cmd.startswith("__is_started"):
                return self.is_started and 1 or 0
            elif cmd.startswith("__start_target"):
                return self.start()
            elif cmd.startswith("__stop_target"):
                return self.stop()

            return self.encodeBytes("", buf, buf_len)
        except:
            return self.encodeBytes("E00", buf, buf_len)

    def gdb(self, port):
        def cmd_cb_hook(cmd, buf, buf_len):
            return self.cmd_cb(cmd, buf, buf_len)

        self.cmd_func_ptr = self.get_cable().cmd_func_typ(cmd_cb_hook)
        self.gdb_handle = self.module.gdb_server_open(
            self.get_cable().get_instance(),
            port,
            self.cmd_func_ptr,
            self.capabilities_str)
        return 0

    def wait(self):
        if self.gdb_handle is not None:
            self.module.gdb_server_close(self.gdb_handle, 0)

        # The wait function returns in case ioloop has been launched
        # as it will check for end of application.
        # Otherwise it will wait for reqloop for ever
        if self.ioloop_handle is not None:
            return self.module.bridge_ioloop_close(self.ioloop_handle, 0)

        if self.reqloop_handle is not None:
            self.module.bridge_reqloop_close(self.reqloop_handle, 0)

        return 0

    def lock(self):
        self.get_cable().lock()

    def unlock(self):
        self.get_cable().unlock()
