# -*- coding: utf-8 -*-

# Copyright 2012-2013 AGR Audio, Industria e Comercio LTDA. <contato@moddevices.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, time, logging, copy, json

from os import path

from copy import deepcopy
from datetime import timedelta
from tornado import iostream, ioloop, gen

from mod.settings import (MANAGER_PORT, DEV_ENVIRONMENT, DEV_HMI, DEV_HOST,
                          HMI_SERIAL_PORT, HMI_BAUD_RATE, CLIPMETER_URI, PEAKMETER_URI, HOST_CARLA,
                          CLIPMETER_IN, CLIPMETER_OUT, CLIPMETER_L, CLIPMETER_R, PEAKMETER_IN, PEAKMETER_OUT,
                          CLIPMETER_MON_R, CLIPMETER_MON_L, PEAKMETER_MON_VALUE_L, PEAKMETER_MON_VALUE_R, PEAKMETER_MON_PEAK_L,
                          PEAKMETER_MON_PEAK_R, PEAKMETER_L, PEAKMETER_R, TUNER, TUNER_URI, TUNER_MON_PORT, TUNER_PORT)
from mod import get_hardware
from mod.bank import get_last_bank_and_pedalboard
from mod.development import FakeHost, FakeHMI
from mod.hmi import HMI
from mod.clipmeter import Clipmeter
from mod.recorder import Recorder, Player
from mod.screenshot import ScreenshotGenerator
from mod.tuner import NOTES, FREQS, find_freqnotecents

if HOST_CARLA:
    from mod.host_carla import Host
else:
    from mod.host import Host

class Session(object):
    def __init__(self):
        self._tuner = False
        self._tuner_port = 1
        self._peakmeter = False

        self.monitor_server = None

        self.ioloop = ioloop.IOLoop.instance()

        self.recorder = Recorder()
        self.player = Player()
        self.mute_state = True
        self.recording = None
        self.screenshot_generator = ScreenshotGenerator()
        self.websockets = []

        # Used in mod-app to know when the current pedalboard changed
        self.pedalboard_changed_callback = lambda ok,bundlepath,title:None

        # Try to open real HMI
        hmiOpened = False

        if not DEV_HMI:
            self.hmi  = HMI(HMI_SERIAL_PORT, HMI_BAUD_RATE, self.hmi_initialized_cb)
            hmiOpened = self.hmi.sp is not None

        print("Using HMI =>", hmiOpened)

        if not hmiOpened:
            self.hmi = FakeHMI(HMI_SERIAL_PORT, HMI_BAUD_RATE, self.hmi_initialized_cb)

        if DEV_HOST:
            self.host = FakeHost(self.hmi, self.msg_callback)
        else:
            self.host = Host(self.hmi, self.msg_callback)

        self._clipmeter = Clipmeter(self.hmi)

    def signal_disconnect(self):
        sockets = self.websockets
        self.websockets = []
        for ws in sockets:
            ws.write_message("stop")
            ws.close()
        self.host.end_session(lambda r:None)

    def get_hardware(self):
        hw = deepcopy(get_hardware())
        hw["addressings"] = self.host.get_addressings()
        return hw

    # -----------------------------------------------------------------------------------------------------------------
    # App utilities, needed only for mod-app

    def setupApp(self, pedalboardChangedCallback):
        self.pedalboard_changed_callback = pedalboardChangedCallback

    def reconnectApp(self):
        if self.host.readsock is not None:
            self.host.readsock.close()
            self.host.readsock = None
        if self.host.writesock is not None:
            self.host.writesock.close()
            self.host.writesock = None
        self.host.open_connection_if_needed(self.websockets[0])

    # -----------------------------------------------------------------------------------------------------------------
    # Initialization

    @gen.coroutine
    def hmi_initialized_cb(self):
        logging.info("hmi initialized")
        self.hmi.initialized = True

        bank_id, pedalboard = get_last_bank_and_pedalboard()

        if pedalboard:
            self.host._load_addressings(pedalboard)
        else:
            bank_id = -1
            pedalboard = ""

        yield gen.Task(self.hmi.initial_state, bank_id, pedalboard, "")

    # -----------------------------------------------------------------------------------------------------------------
    # Webserver callbacks, called from the browser (see webserver.py)
    # These will be called as a reponse to an action in the browser.
    # A callback must always be used unless specified otherwise.

    # Add a new plugin, starts enabled (ie, not bypassed)
    def web_add(self, instance, uri, x, y, callback):
        self.host.add_plugin(instance, uri, x, y, callback)

    # Remove a plugin
    def web_remove(self, instance, callback):
        self.host.remove_plugin(instance, callback)

    # Set a plugin parameter
    # We use ":bypass" symbol for on/off state
    def web_parameter_set(self, port, value, callback):
        instance, portsymbol = port.rsplit("/",1)

        if portsymbol == ":bypass":
            value = value >= 0.5
            self.host.bypass(instance, value, callback)
        else:
            self.host.param_set(port, value, callback)

        #self.recorder.parameter(port, value)

    # Address a plugin parameter
    def web_parameter_address(self, port, actuator_uri, label, maximum, minimum, value, steps, callback):
        if not (self.hmi.initialized or actuator_uri.startswith("/midi-")):
            callback(False)
            return

        instance, port2 = port.rsplit("/",1)
        self.host.address(instance, port2, actuator_uri, label, maximum, minimum, value, steps, callback)

    # Set a parameter for MIDI learn
    def web_parameter_midi_learn(self, port, callback):
        self.host.midi_learn(port, callback)

    # Load a plugin preset
    def web_preset_load(self, instance, uri, callback):
        self.host.preset_load(instance, uri, callback)

    # Save a plugin preset
    def web_preset_save(self, instance, name, callback):
        self.host.preset_save(instance, name, callback)

    # Set a plugin block position within the canvas
    def web_set_position(self, instance, x, y):
        self.host.set_position(instance, x, y)

    # Connect 2 ports
    def web_connect(self, port_from, port_to, callback):
        self.host.connect(port_from, port_to, callback)

    # Disconnect 2 ports
    def web_disconnect(self, port_from, port_to, callback):
        self.host.disconnect(port_from, port_to, callback)

    # Save the current pedalboard
    # returns saved bundle path
    def web_save_pedalboard(self, title, asNew):
        bundlepath = self.host.save(title, asNew)
        self.pedalboard_changed_callback(True, bundlepath, title)
        self.screenshot_generator.schedule_screenshot(bundlepath)
        return bundlepath

    # Get list of Hardware MIDI devices
    # returns (devsInUse, devList)
    def web_get_midi_device_list(self):
        return self.host.get_midi_ports()

    # Set the selected MIDI devices to @a newDevs
    # Will remove or add new JACK ports as needed
    def web_set_midi_devices(self, newDevs):
        return self.host.set_midi_devices(newDevs)

    # Send a ping to HMI
    def web_ping_hmi(self, callback):
        self.hmi.ping(callback)

    # A new webbrowser page has been open
    # We need to cache its socket address and send any msg callbacks to it
    def websocket_opened(self, ws, callback):
        def ready(ok):
            self.websockets.append(ws)
            self.host.open_connection_if_needed(ws)
            callback(True)

        # if this is the 1st socket, start ui session
        if len(self.websockets) == 0:
            self.host.start_session(ready)
        else:
            ready(True)

    # Webbrowser page closed
    def websocket_closed(self, ws, callback):
        try:
            self.websockets.remove(ws)
        except ValueError as e:
            pass

        # if this is the last socket, end ui session
        if len(self.websockets) == 0:
            self.host.end_session(callback)
        else:
            callback(True)

    # -----------------------------------------------------------------------------------------------------------------
    # TODO
    # Everything after this line is yet to be documented

    def msg_callback(self, msg):
        for ws in self.websockets:
            ws.write_message(msg)

    def load_pedalboard(self, bundlepath, bank_id=-1):
        title = self.host.load(bundlepath, bank_id)
        self.pedalboard_changed_callback(True, bundlepath, title)
        return title

    def reset(self, callback):
        # Callback from HMI, ignore ok status
        def reset_host(ok):
            self.host.reset(callback)

        # Wait for HMI if available
        if self.hmi.initialized:
            self.hmi.clear(reset_host)
        else:
            reset_host(True)

        self.pedalboard_changed_callback(True, "", "")

    #def setup_monitor(self):
        #if self.monitor_server is None:
            #from mod.monitor import MonitorServer
            #self.monitor_server = MonitorServer()
            #self.monitor_server.listen(12345)

            #self.set_monitor("localhost", 12345, 1, self.add_tools)

    #def add_tools(self, resp):
        #if resp:
            #self.add(CLIPMETER_URI, CLIPMETER_IN, self.setup_clipmeter_in, True)
            #self.add(CLIPMETER_URI, CLIPMETER_OUT, self.setup_clipmeter_out, True)

    #def setup_clipmeter_in(self, resp):
        #if resp:
            #self.connect("system:capture_1", "effect_%d:%s" % (CLIPMETER_IN, CLIPMETER_L), lambda r:None, True)
            #self.connect("system:capture_2", "effect_%d:%s" % (CLIPMETER_IN, CLIPMETER_R), lambda r:None, True)
            #self.parameter_monitor(CLIPMETER_IN, CLIPMETER_MON_L, ">=", 0, lambda r:None)
            #self.parameter_monitor(CLIPMETER_IN, CLIPMETER_MON_R, ">=", 0, lambda r:None)

    #def setup_clipmeter_out(self, resp):
        #if resp:
            #self.parameter_monitor(CLIPMETER_OUT, CLIPMETER_MON_L, ">=", 0, lambda r:None)
            #self.parameter_monitor(CLIPMETER_OUT, CLIPMETER_MON_R, ">=", 0, lambda r:None)

    # host commands

    def bypass(self, instance, value, callback):
        value = int(value) > 0
        #if not loaded:
        #    self._pedalboard.bypass(instance_id, value)
        #self.recorder.bypass(instance, value)
        self.host.enable(instance, value, callback)

    def format_port(self, port):
        if not 'system' in port and not 'effect' in port:
            port = "effect_%s" % port
        return port

    #def set_monitor(self, addr, port, status, callback):
        #self.host.monitor(addr, port, status, callback)

    #def parameter_monitor(self, instance_id, port_id, op, value, callback):
        #self.host.param_monitor(instance_id, port_id, op, value, callback)

    # END host commands

    def pedalboard_size(self, width, height):
        self.host.set_pedalboard_size(width, height)

    def clipmeter(self, pos, value):
        self._clipmeter.set(pos, value)

    def peakmeter(self, pos, value, peak, callback):
        self.hmi.peakmeter(pos, value, peak, callback)

    def tuner(self, value, callback):
        freq, note, cents = find_freqnotecents(value)
        self.hmi.tuner(freq, note, cents, callback)

    def start_recording(self):
        if self.player.playing:
            self.player.stop()
        self.recorder.start()

    def stop_recording(self):
        if self.recorder.recording:
            self.recording = self.recorder.stop()
            return self.recording

    def start_playing(self, stop_callback):
        if self.recorder.recording:
            self.recording = self.recorder.stop()
        def stop():
            self.unmute(stop_callback)
        def schedule_stop():
            self.ioloop.add_timeout(timedelta(seconds=0.5), stop)
        def play():
            self.player.play(self.recording['handle'], schedule_stop)
        self.mute(play)

    def stop_playing(self):
        self.player.stop()

    def reset_recording(self):
        self.recording = None

    def mute(self, callback):
        return
        #self.set_audio_state(False, callback)

    def unmute(self, callback):
        return
        #self.set_audio_state(True, callback)

    #def set_audio_state(self, state, callback):
        #if self.mute_state == state:
            #return callback()
        #self.mute_state = state
        #connections = self._pedalboard.data['connections']
        #queue = []
        #for connection in connections:
            #if connection[2] == 'system' and connection[3].startswith('playback'):
                #port_from = self.format_port(':'.join([str(x) for x in connection[:2]]))
                #port_to = self.format_port(':'.join([str(x) for x in connection[2:]]))
                #queue.append([port_from, port_to])
        #def consume(result=None):
            #if len(queue) == 0:
                #return callback()
            #nxt = queue.pop(0)
            #if state:
                #self.host.connect(nxt[0], nxt[1], consume)
            #else:
                #self.host.disconnect(nxt[0], nxt[1], consume)
        #consume()

    #def serialize_pedalboard(self):
        #return self._pedalboard.serialize()

    #def xrun(self, callback):
        #self.hmi.xrun(callback)

SESSION = Session()
