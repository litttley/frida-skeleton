#!/usr/bin/env python
# -*- coding: utf-8 -*-

import lzma
import os
import re
import threading
import time

import frida

from lib.core.common import download
from lib.core.log import LOGGER
from lib.core.settings import ROOT_DIR
from lib.utils.adb import Adb
from lib.utils.iptables import Iptables

__lock__ = threading.Lock()


class FridaThread(threading.Thread):

    def __init__(self, device, install: bool, port: int, regexps: list, daemon: bool):
        super().__init__(daemon=daemon)

        self.device = device
        self.install = install
        self.port = port
        self.regexps = regexps if regexps else ['.*']

        self.adb = Adb(self.device.id)
        self.iptables = Iptables(self.adb)

        self.arch = self.adb.unsafe_shell("getprop ro.product.cpu.abi")['out']
        # maybe get 'arm64-v8a', 'arm-v7a' ...
        if 'arm64' in self.arch:
            self.arch = 'arm64'
        elif 'arm' in self.arch:
            self.arch = 'arm'
        elif 'x86_64' in self.arch:
            self.arch = 'x86_64'
        elif 'x86' in self.arch:
            self.arch = 'x86'
        else:
            raise RuntimeError('unknown arch: ' + self.arch)

        self.server_name = 'frida-server-{}-android-{}'.format(frida.__version__, self.arch)

    def run(self) -> None:
        LOGGER.info("{} start with hook device: id={}, name={}, type={}".format(
            self.__class__.__name__, self.device.id, self.device.name, self.device.type))

        try:
            self.prepare()
            self.hook_apps()
        except Exception as e:
            LOGGER.error(e)

    # prepare for starting hook
    def prepare(self):
        # get root
        self.adb.root()

        # close selinux
        self.adb.unsafe_shell('setenforce 0', root=True)

        self.iptables.uninstall()

        # install iptables and reverse tcp port
        if self.port:
            # enable tcp connections between frida server and binding
            self.iptables.install(self.port)
            self.adb.reverse(self.port)

        if self.install:
            self.install_frida_server()

        self.kill_frida_servers()
        self.run_frida_server()

    def install_frida_server(self):
        server_path = os.path.join(ROOT_DIR, 'assets', self.server_name)
        server_path_xz = server_path + '.xz'

        # if not exist frida server then install it
        if not self.adb.unsafe_shell("ls /data/local/tmp/" + self.server_name)['out']:
            LOGGER.info('download {} from github ...'.format(self.server_name))
            with __lock__:
                download('https://github.com/frida/frida/releases/download/{}/{}.xz'
                         .format(frida.__version__, self.server_name), server_path_xz)

            # extract frida server
            with open(server_path, 'wb') as f:
                with lzma.open(server_path_xz) as xz:
                    f.write(xz.read())

            # upload frida server
            self.adb.push(server_path, '/data/local/tmp/')

    def kill_frida_servers(self):
        try:
            apps = self.device.enumerate_processes()
        except frida.ServerNotRunningError:
            # frida server has not been started, no need to start
            return

        for app in apps:
            if app.name == self.server_name:
                self.device.kill(app.pid)

    def run_frida_server(self):
        self.adb.unsafe_shell('chmod +x /data/local/tmp/' + self.server_name)
        threading.Thread(
            target=self.adb.unsafe_shell,
            args=('/data/local/tmp/{} -D'.format(self.server_name), True)
        ).start()

        # waiting for frida server
        time.sleep(0.5)

    def hook_apps(self):
        apps = set()

        # monitor apps
        while True:
            time.sleep(0.1)

            new_apps = set(p.name for p in self.device.enumerate_processes())
            if not new_apps:
                continue

            incremental_apps = new_apps - apps
            decremental_apps = apps - new_apps

            for incremental_app in incremental_apps:
                for regexp in self.regexps:
                    if re.search(regexp, incremental_app):
                        # waiting for app startup completely
                        time.sleep(0.1)

                        try:
                            self.hook(incremental_app)
                        except Exception as e:
                            LOGGER.error(e)
                        finally:
                            break

            for decremental_app in decremental_apps:
                for regexp in self.regexps:
                    if re.search(regexp, decremental_app):
                        LOGGER.info('app {} has died'.format(decremental_app))
                        break

            apps = new_apps

    def hook(self, app: str):
        app = app.strip()
        if not app:
            raise RuntimeError('try to hook empty app name')

        LOGGER.info('hook app ' + app)
        process = self.device.attach(app)
        js = 'Java.perform(function() {'

        # load all scripts under folder 'scripts'
        for (dirpath, dirnames, filenames) in os.walk(os.path.join(ROOT_DIR, 'scripts')):
            for filename in filenames:
                _ = open(os.path.join(dirpath, filename), encoding="utf-8").read()
                if _.startswith(r'''/*Deprecated*/'''):
                    continue
                js += _
                js += '\n'

        js += '});'
        script = process.create_script(js)
        script.on('message', self.on_message)
        script.load()

    def on_message(self, message, data):
        try:
            if message['type'] == 'error':
                text = message['description'].strip()

                if not text:
                    return

                LOGGER.error(text)
            else:
                text = message['payload'].strip() if message['type'] == 'send' else message.strip()

                if not text:
                    return

                LOGGER.info(text)
        except Exception as e:
            LOGGER.error(e)
