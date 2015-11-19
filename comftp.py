"""Simple aioftp-based server with anonymous user and pathio
virtualization layer via serial port.

Usage:
    comftp list-coms
    comftp [options]

Options:
    -q, --quiet                     set logging level to "ERROR" instead of
                                    "INFO"
    --host=host                     host for binding [default: 127.0.0.1]
    --port=port                     port for binding [default: 8021]
    --serial-port=serial-port       serial port for binding
    --serial-speed=serial-speed     speed of serial port connection
                                    [default: 115200]
    --ftrans-send=template          ftrans xmodem send template
                                    [default: f /s {filename}]
    --ftrans-size=template          ftrans xmodem send template size adder
                                    [default: {size}]
    --ftrans-receive=template       ftrans xmodem receive template
                                    [default: f {filename}]
"""
import asyncio
import logging
import functools
import pathlib
import string
import datetime
import time
import collections

import docopt
import serial
import serial.tools.list_ports
import aioftp


# EOL = str.encode("\r\n", "utf-8")
EOL = str.encode("\r", "utf-8")
SOH = b"\x01"
STX = b"\x02"
EOT = b"\x04"
ACK = b"\x06"
DLE = b"\x10"
NAK = b"\x15"
ETB = b"\x17"
CAN = b"\x18"
CRC = b"C"


class ComFtpServer(aioftp.Server):

    @aioftp.ConnectionConditions(aioftp.ConnectionConditions.login_required)
    @asyncio.coroutine
    def allo(self, connection, rest):

        try:

            connection.path_io.allocate_size = int(rest)

        except ValueError:

            connection.path_io.allocate_size = None

        connection.response("200", "size argument will be passed")

        return True


class AioSerial(serial.Serial):

    def __init__(self, *args, loop=None, **kwargs):

        super().__init__(*args, **kwargs)
        self.loop = loop or asyncio.get_event_loop()
        self._data = collections.deque()
        self.aread_lock = asyncio.Lock(loop=self.loop)
        self.reader_task = self.loop.create_task(self.reader())

    @asyncio.coroutine
    def reader(self):

        while True:

            byte = yield from loop.run_in_executor(None, self.read)
            self._data.append(byte)
            if self.aread_lock.locked():

                self.aread_lock.release()

    def close(self):

        super().close()
        self.reader_task.cancel()

    @asyncio.coroutine
    def aread(self, size=1, *, timeout=None):

        @asyncio.coroutine
        def read():

            while len(self._data) < size:

                yield from self.aread_lock.acquire()

            bs = map(lambda _: self._data.popleft(), range(size))
            data = bytes.join(b"", bs)
            return data

        try:

            data = yield from asyncio.wait_for(read(), timeout)

        except asyncio.TimeoutError:

            data = None

        return data

    @asyncio.coroutine
    def read_until(self, expected_tail=">", *, timeout=None):

        size = len(expected_tail)
        if isinstance(expected_tail, str):

            expected_tail = str.encode(expected_tail, "utf-8")

        message = b""
        while message[-size:] != expected_tail:

            data = yield from self.aread(timeout=timeout)
            if data is None:

                return

            message += data

        return message[:-size]

    @asyncio.coroutine
    def read_until_timeout(self, timeout):

        while (yield from self.aread(timeout=timeout)) is not None:

            pass

    @asyncio.coroutine
    def init(self):

        while True:

            # ctrl-b to bypass autoexec
            self.write(b"\x02")
            yield from asyncio.sleep(0.5)
            self.write(EOL)
            try:

                yield from asyncio.wait_for(self.read_until(), 0.1)
                break

            except asyncio.TimeoutError:

                continue

        yield from asyncio.sleep(0.1)
        self.write(EOL)
        yield from asyncio.sleep(0.1)
        r = yield from self.read_until()
        print("success dos initialization")


class SerialPathIO(aioftp.AbstractPathIO):

    def __init__(self, serial, *args, send_template, size_template,
                 receive_template, **kwargs):

        super().__init__(*args, **kwargs)
        self.serial = serial
        self.send_template = send_template
        self.receive_template = receive_template
        self.receive_size_template = size_template
        self.root = pathlib.Path("/")
        self.cach = {}
        self.allocate_size = None

    def _prepare_path(self, path):

        root, disk, *path = map(str.lower, path.parts)
        dos_path = str.format("{}:\\{}", disk, str.join("\\", path))
        return dos_path

    @asyncio.coroutine
    def _do_command_erase(self, size):

        yield from self.serial.read_until_timeout(0.1)
        self.serial.write(b"\x08" * size)
        yield from self.serial.read_until_timeout(0.1)

    @asyncio.coroutine
    def _do_command(self, command, expected_tail=":\\>"):

        blob = None
        while not blob:

            for ch in command:

                self.serial.write(bytes([ch]))
                check_ch = yield from self.serial.aread(timeout=0.25)
                if check_ch is None or ch != check_ch[0]:

                    yield from self._do_command_erase(len(command) * 2)
                    break

            else:

                if (yield from self.serial.aread(timeout=0.1)):

                    self.serial.flushInput()
                    yield from self._do_command_erase(len(command) * 2)

                else:

                    self.serial.write(EOL)
                    data = yield from self.serial.read_until("\n", timeout=0.1)
                    if data is not None:

                        blob = yield from self.serial.read_until(expected_tail)

                    else:

                        yield from self._do_command_erase(len(command) * 2)

        return blob

    @asyncio.coroutine
    def exists(self, path):

        if path == self.root:

            return True

        elif len(path.parts) == 2:

            disks = yield from self.list(self.root)
            return path in disks

        else:

            dos_path = self._prepare_path(path.parent)
            for name, is_dir, *_ in (yield from self._dir(dos_path)):

                if name == str.lower(path.name):

                    return True

            return False

    @asyncio.coroutine
    def is_dir(self, path):

        if len(path.parts) < 3:

            return True

        else:

            dos_path = self._prepare_path(path.parent)
            for name, is_dir, *_ in (yield from self._dir(dos_path)):

                if name == str.lower(path.name):

                    return is_dir

        raise ValueError

    @asyncio.coroutine
    def is_file(self, path):

        return not (yield from self.is_dir(path))

    @asyncio.coroutine
    def mkdir(self, path, *, parents=False):

        paths_to_create = []
        while not (yield from self.exists(path)):

            paths_to_create.append(path)
            path = path.parent

        if paths_to_create:

            arg = self._prepare_path(path)
            if arg in self.cach:

                self.cach.pop(arg)

            for path in paths_to_create:

                dos_path = self._prepare_path(path)
                command = b"md " + str.encode(dos_path, "utf-8")
                yield from self._do_command(command)

    @asyncio.coroutine
    def rmdir(self, path):

        if len(path.parts) > 2:

            arg = self._prepare_path(path.parent)
            if arg in self.cach:

                self.cach.pop(arg)

            dos_path = self._prepare_path(path)
            command = b"rd " + str.encode(dos_path, "utf-8")
            yield from self._do_command(command)

    @asyncio.coroutine
    def unlink(self, path):

        if len(path.parts) > 2:

            arg = self._prepare_path(path.parent)
            if arg in self.cach:

                self.cach.pop(arg)

            dos_path = self._prepare_path(path)
            command = b"del " + str.encode(dos_path, "utf-8")
            yield from self._do_command(command)

    def _parse_dir_file_result(self, line):

        name, ext, size, date = map(
            str.strip,
            (
                line[:8],
                line[9:12],
                line[13:26],
                line[26:36],
            )
        )
        if ext:

            name = name + "." + ext

        name = str.lower(name)
        is_dir = size == "<DIR>"
        if is_dir:

            size = 0

        else:

            size = int(str.replace(size, ",", ""))

        date = datetime.datetime.strptime(date, "%m-%d-%y")
        return name, is_dir, size, date

    @asyncio.coroutine
    def _dir(self, arg):

        if arg not in self.cach:

            command = b"dir " + str.encode(arg, "utf-8")
            blob = yield from self._do_command(command)
            s = bytes.decode(blob, "utf-8")
            if "File not found" in s or "Invalid drive" in s:

                return

            lines = tuple(map(str.strip, str.split(s, "\n")))
            files = lines[5:-3]
            all_info = map(self._parse_dir_file_result, files)
            files_info = filter(lambda i: i[0] not in (".", ".."), all_info)
            self.cach[arg] = tuple(files_info)

        return self.cach[arg]

    @asyncio.coroutine
    def list(self, path):

        r = []
        if path == self.root:

            for label in string.ascii_lowercase[2:]:

                info = yield from self._dir(label + ":")
                if not info:

                    break

                r.append(path / label)

        else:

            dos_path = self._prepare_path(path)
            for name, *_ in (yield from self._dir(dos_path)):

                r.append(path / name)

        return r

    @asyncio.coroutine
    def stat(self, path):

        if len(path.parts) < 3:

            stats = aioftp.MemoryPathIO.Stats(0, 0, 0, 1, 0o100777)

        else:

            dos_path = self._prepare_path(path.parent)
            for name, is_dir, size, date in (yield from self._dir(dos_path)):

                if name == str.lower(path.name):

                    t = time.mktime(date.timetuple())
                    stats = aioftp.MemoryPathIO.Stats(size, t, t, 1, 0o100777)

        return stats

    @asyncio.coroutine
    def open(self, path, mode):

        self.file_mode = mode
        self.data_buffer = b""
        self.seq = 1

        dos_path = self._prepare_path(path)

        if mode == "rb":

            self.transfer_size, *_ = yield from self.stat(path)
            command = str.encode(
                str.format(
                    self.send_template,
                    filename=dos_path
                )
            )
            yield from self._do_command(command, " ... ")
            self.serial.write(NAK)

        elif mode == "wb":

            arg = self._prepare_path(path.parent)
            if arg in self.cach:

                self.cach.pop(arg)

            if self.allocate_size is not None:

                template = (
                    self.receive_template +
                    " " +
                    self.receive_size_template
                )

            else:

                template = self.receive_template

            command = str.encode(
                str.format(
                    template,
                    filename=dos_path,
                    size=self.allocate_size
                )
            )
            yield from self._do_command(command, " ... ")
            yield from self.serial.read_until(NAK)

        else:

            raise NotImplementedError

        return path

    @asyncio.coroutine
    def write(self, file, data):

        self.data_buffer += data
        while len(self.data_buffer) >= 128:

            data = self.data_buffer[:128]
            self.data_buffer = self.data_buffer[128:]
            while True:

                self.serial.write(SOH)
                self.serial.write(bytes([self.seq, 0xff - self.seq]))
                self.serial.write(data)
                self.serial.write(bytes([sum(data) % 0x100]))
                answer = yield from self.serial.aread()
                if answer == ACK:

                    self.seq = (self.seq + 1) % 0x100
                    break

    @asyncio.coroutine
    def read(self, *args):

        ok = False
        while not ok:

            mode = yield from self.serial.aread(1)
            seq = yield from self.serial.aread(2)

            if mode == SOH:

                data = yield from self.serial.aread(128)

            elif mode == STX:

                data = yield from self.serial.aread(1024)

            elif mode == EOT:

                data = b""
                break

            csum = yield from self.serial.aread(1)
            ok = csum[0] == (sum(data) % 0x100)
            if not ok:

                self.serial.write(NAK)

        self.serial.write(ACK)
        data = data[:self.transfer_size]
        self.transfer_size -= len(data)
        return data

    @asyncio.coroutine
    def close(self, *args):

        if self.file_mode == "wb":

            yield from self.write(None, b"0" * (128 - len(self.data_buffer)))
            self.serial.write(EOT)
            yield from self.serial.aread()  # ACK
            self.serial.write(ETB)

    @asyncio.coroutine
    def rename(self, source, destination):

        if len(source.parts) > 2:

            for path in (source, source.parent):

                arg = self._prepare_path(path)
                if arg in self.cach:

                    self.cach.pop(arg)

            dos_path = self._prepare_path(source)
            new_name = destination.name
            command = b"ren " + str.encode(dos_path, "utf-8") + b" " + \
                str.encode(new_name, "utf-8")
            yield from self._do_command(command)


if __name__ == "__main__":

    args = docopt.docopt(__doc__, version=aioftp.__version__)

    ports = sorted(serial.tools.list_ports.comports())
    if args["list-coms"]:

        for name, _, _ in ports:

            try:

                print(name)

            except:

                print("can't show com name")

        exit()

    if not args["--quiet"]:

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s",
            datefmt="[%H:%M:%S]:",
        )

    com = args["--serial-port"] or next(iter(ports))[0]
    print(str.format("Using '{}'", com))
    loop = asyncio.get_event_loop()
    s = AioSerial(
        com,
        baudrate=int(args["--serial-speed"]),
        # parity=serial.PARITY_ODD,
        loop=loop,
    )

    loop.run_until_complete(s.init())

    path_io_factory = functools.partial(
        SerialPathIO,
        s,
        send_template=args["--ftrans-send"],
        size_template=args["--ftrans-size"],
        receive_template=args["--ftrans-receive"]
    )

    print(str.format("aioftp v{}", aioftp.__version__))
    user = aioftp.User(base_path="/")
    server = ComFtpServer(users=[user], path_io_factory=path_io_factory)
    loop.run_until_complete(server.start(args["--host"], int(args["--port"])))
    try:

        loop.run_forever()

    except KeyboardInterrupt:

        server.close()
        loop.run_until_complete(server.wait_closed())
        s.close()
        loop.close()
