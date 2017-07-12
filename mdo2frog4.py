#!/usr/bin/env python
from sys import maxint
from exception import ClientError, ServerError
from collections import OrderedDict
from requests.exceptions import HTTPError
__author__ = 'Ivano Cerrato, Stefano Petrangeli'
__modified_by__ = 'Francesco Lubrano'

import falcon
import json
import logging as LOG
import requests
import ConfigParser
import re
import os

import constants
from virtualizer_library.virtualizer import ET, Virtualizer,  Software_resource, Infra_node, Port as Virt_Port
from nffg_library.nffg import NF_FG, VNF, Match, Action, EndPoint, FlowRule, Port, UnifyControl

class DoPing:
	'''
	This class manages the ping
	'''

	def on_get(self,req,resp):
		resp.body = "OK"
		resp.status = falcon.HTTP_200

	def on_post(self,req,resp):
		resp.body = "OK"
		resp.status = falcon.HTTP_200

class DoUsage:
	'''
	This class shows how to interact with the mdo2frog4
	'''

	def __init__(self):
		a = 'usage:\n'
		b = '\tget http://hostip:tcpport - this help message\n'
		c = '\tget http://hostip:tcpport/ping - test webserver aliveness\n'
		d = '\tpost http://hostip:tcpport/get-config - query NF-FG\n'
		e = '\tpost http://hostip:tcpport/edit-config - send NF-FG request in the post body\n'
		f = '\n'
		g = 'limitations (of the mdo2frog4 ):\n'
		h = '\tthe flowrule ID must be unique on the node.\n'
		i = '\ttype cannot be repeated in multiple NF instances.\n'
		j = '\tcapabilities are not supported.\n'
		k = '\tit is not possible to deploy a VNF that does not have any flow referring its ports.\n'
		l = '\ta VNF is removed (undeployed) only when no flows still remain that refer to its ports.\n'
		m = '\tthe number of ports actually attached to a VNF depends on the number of ports used in the flows'
		N = '\n\n'
		self._answer = a + b + c + d + e + f + g + h + i + j + k + l + m + N

	def on_get(self,req,resp):
		resp.body = self._answer
		resp.status = falcon.HTTP_200

	def on_post(self,req,resp):
		resp.body = self._answer
		resp.status = falcon.HTTP_200

class DoGetConfig:

	def on_post(self,req,resp):
		'''
		Return the current configuration of the node.
		'''
		LOG.info("Executing the 'get-config' command")
		LOG.debug("Reading file: %s",constants.GRAPH_XML_FILE)

		try:
			tree = ET.parse(constants.GRAPH_XML_FILE)
		except ET.ParseError as e:
			print('ParseError: %s' % e.message)
			LOG.error('ParseError: %s', e.message)
			resp.status = falcon.HTTP_500
			return

		LOG.debug("File correctly read")

		infrastructure = Virtualizer.parse(root=tree.getroot())

		LOG.debug("%s",infrastructure.xml())

		resp.content_type = "text/xml"
		resp.body = infrastructure.xml()
		resp.status = falcon.HTTP_200

		LOG.info("'get-config' command properly handled")

class DoEditConfig:

	def on_post(self, req, resp):
		'''
		Edit the status of the domain
		'''
		global unify_monitoring, fakevm
		try:
			LOG.info("Executing the 'edit-config' command")
			# check if this request comes from Escape
			content = req.stream.read()
			LOG.debug("Request coming from Mdo: ")
			LOG.debug("%s", content)
			content = adjustEscapeNffg(content)
			#content = req.stream.read()
			#content = req
			#LOG.debug("Body of the request after adjust:")
			#LOG.debug("%s",content)
			#checkCorrectness(content)
			#LOG.debug("Body of the request after check:")
			#LOG.debug("%s",content)

			# Extract the needed information from the message received from the network

			vnfsToBeAdded = extractVNFsInstantiated(content)	#VNF deployed/to be deployed on the controlled domain (eg. frog4 orchestrator)
			rulesToBeAdded, endpoints = extractRules(content)	#Flowrules and endpoints installed/to be installed on the controlled domain (eg. frog4 orchestrator)

			# Interact with the frog4 orchestrator in order to implement the required commands
			# Fakevm is a way to deploy and connect just 2 endpoints in a onos_domain without nf instance.
			if fakevm is True:
				fakevm = False
				rulesToBeAdded = connectEndpoints(rulesToBeAdded)

			sendToOrchestrator(rulesToBeAdded,vnfsToBeAdded, endpoints)	#Sends the new VNFs and flow rules to the controlled domain (eg. frog4 orchestrator)
			
			#Updates the file containing the current status of the controlled domain (eg. frog4 orchestrator), by editing the #<flowtable> and the <NF_instances> and returning the xml
			un_config = updateDomainStatus(content)

			resp.content_type = "text/xml"
			resp.body = un_config
			resp.status = falcon.HTTP_200

			unify_monitoring = ""

			LOG.info("'edit-config' command properly handled")

		except ClientError:
			resp.status = falcon.HTTP_400
		except ServerError:
			LOG.error("Please, press 'ctrl+c' and restart the mdo2frog4.")
			resp.status = falcon.HTTP_500
		except Exception as err:
			LOG.exception(err)
			resp.status = falcon.HTTP_500

def checkCorrectness(newContent):
	'''
	Check if the new configuration of the node (in particular, the flowtable) is correct:
	*	the ports are part of the underlying domain
	*	the VNFs referenced in the flows are instantiated
	'''

	LOG.debug("Checking the correctness of the new configuration...")

	LOG.debug("Reading file '%s', which contains the current status of the controlled domain...",constants.GRAPH_XML_FILE)
	try:
		oldTree = ET.parse(constants.GRAPH_XML_FILE)
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ServerError("ParseError: %s" % e.message)
	LOG.debug("File correctly read")

	infrastructure = Virtualizer.parse(root=oldTree.getroot())
	domain = infrastructure.nodes.node[constants.NODE_ID]
	flowtable = domain.flowtable
	nfInstances = domain.NF_instances

	LOG.debug("Getting the new flowrules to be sent to the controlled domain (eg. frog4 orchestrator)")
	try:
		newTree = ET.ElementTree(ET.fromstring(newContent))
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ClientError("ParseError: %s" % e.message)

	newInfrastructure = Virtualizer.parse(root=newTree.getroot())
	newFlowtable = newInfrastructure.nodes.node[constants.NODE_ID].flowtable
	newNfInstances = newInfrastructure.nodes.node[constants.NODE_ID].NF_instances

	#Update the NF instances with the new NFs
	try:
		for instance in newNfInstances:
			if instance.get_operation() == 'delete':
				nfInstances[instance.id.get_value()].delete()
			else:
				nfInstances.add(instance)
	except KeyError:
		raise ClientError("Trying to delete a VNF that does not exist! ID: " + instance.id.get_value())

	#Update the flowtable with the new flowentries
	try:
		for flowentry in newFlowtable:
			if flowentry.get_operation() == 'delete':
				flowtable[flowentry.id.get_value()].delete()
			else:
				flowtable.add(flowentry)
	except KeyError:
		LOG.error("Trying to delete a flowrule that does not exist! ID:%s", flowentry.id.get_value())
		raise ClientError("Trying to delete a flowrule that does not exist! ID: "+ flowentry.id.get_value())

	#Here, infrastructure contains the new configuration of the node
	#Then, we execute the checks on it!


	LOG.debug("The new status of the domain is correct!")

def extractVNFsInstantiated(content):
	'''
	Parses the message and extracts the type of the deployed network functions.
	'''

	global graph_id, tcp_port, unify_port_mapping, unify_monitoring, fakevm

	try:
		tree = ET.parse(constants.GRAPH_XML_FILE)
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ServerError("ParseError: %s" % e.message)

	tmpInfrastructure = Virtualizer.parse(root=tree.getroot())
	supportedNFs = tmpInfrastructure.nodes.node[constants.NODE_ID].capabilities.supported_NFs
	supportedTypes = []
	#lowerPortId = {}
	for nf in supportedNFs:
		nfType = nf.type.get_value()
		supportedTypes.append(nfType)
		#lowerPortId[nfType] = getLowerPortId(nf)

	LOG.debug("Extracting the network functions (to be) deployed on the the controlled domain (eg. frog4 orchestrator)")
	try:
		tree = ET.ElementTree(ET.fromstring(content))
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ClientError("ParseError: %s" % e.message)

	infrastructure = Virtualizer.parse(root=tree.getroot())
	domain = infrastructure.nodes.node[constants.NODE_ID]
	instances = domain.NF_instances

	#foundTypes = []
	nfinstances = []

	LOG.debug("Considering instances:")
	LOG.debug("'%s'",infrastructure.xml())

	for instance in instances:
		if instance.get_operation() is None:
			LOG.warning("Update of VNF is not supported by the UN! vnf: {0}".format(instance.id.get_value()))
			LOG.warning("This VNF will be disregarded by the Orchestrator if it already exists, or created if it does not exist")

			#LOG.error("Update of VNF is not supported by the UN! vnf: " + instance.id.get_value())
			#LOG.error("If you want to create a new instance please set \"operation=create\" to the node element")
			#raise ClientError("Update of VNF is not supported by the UN! vnf: "+instance.id.get_value())

		elif instance.get_operation() == 'delete':
			graph_id=instance.id.get_value()
			continue

		elif instance.get_operation() != 'create':
			LOG.error("Unsupported operation for vnf: " + instance.id.get_value())
			raise ClientError("Unsupported operation for vnf: "+instance.id.get_value())
		graph_id=instance.id.get_value()
		vnfType = instance.name.get_value()

		port_list = []
		#Append the first port dedicated to management vlan
		port_list.append(Port(_id="port:"+str(0)))
		unify_control = []
		unify_env_variables = []
		for port_id in instance.ports.port:
			port = instance.ports[port_id]
			l4_addresses = port.addresses.l4.get_value()
			# only process l4 address for new VNFs to be created
			if l4_addresses is not None and instance.get_operation() == 'create':
				if int(port.id.get_value()) != 0:
					LOG.error("L4 configuration is supported only to the port with id = 0 on VNF of type '%s'", vnfType)
					raise ClientError("L4 configuration is supported only to the port with id = 0 on VNF of type " + vnfType)
				# find all the l4_addresses with regular expression and reformat to request notation "{protocol/port,}"
				# l4_address format can be "protocol/port: (ip, port)" when sending back a existing vnf
				l4_addresses_list = re.findall("('[a-z]*\/\d*')", l4_addresses)
				s= ","
				l4_addresses = s.join(l4_addresses_list)
				# Removing not needed chars
				for ch in ['{','}',' ',"'"]:
					if ch in l4_addresses:
						l4_addresses=l4_addresses.replace(ch,"")
				LOG.debug("l4 adresses: %s", l4_addresses)
				for l4_address in l4_addresses.split(","):
					tmp = l4_address.split("/")
					if tmp[0] != "tcp":
						LOG.error("Only tcp ports are supported on L4 configuration of VNF of type '%s'", vnfType)
						raise ClientError("Only tcp ports are supported on L4 configuration of VNF of type "+ vnfType)
					l4_port = tmp[1]
					uc = UnifyControl(vnf_tcp_port=int(l4_port), host_tcp_port=tcp_port)
					unify_port_mapping[instance.id.get_value() + ":" + port_id + "/" + l4_address] = (OrchestratorIP, tcp_port)
					unify_control.append(uc)
					tcp_port = tcp_port + 1
			# just copy the existing l4 addresses
			elif l4_addresses is not None and instance.get_operation() is None:
				l4_addresses_list = re.findall("'[a-z]*\/(\d*)'\s*:\s*\('[0-9.]*', (\d*)\)", l4_addresses)
				for vnf_port, host_port in l4_addresses_list:
					uc = UnifyControl(vnf_tcp_port=int(int(vnf_port)), host_tcp_port=int(host_port))
					unify_control.append(uc)
			else:
				if int(port.id.get_value()) == 0 :
					LOG.error("Port with id = 0 should be present only if it has a L4 configuration on VNF of type '%s'", vnfType)
					raise ClientError("Port with id = 0 should be present only if it has a L4 configuration on VNF of type " + vnfType)
				unify_ip = None
				if port.addresses.l3.length() != 0:
					if port.addresses.l3.length() > 1:
						LOG.error("Only one l3 address is supported on a port on VNF of type '%s'", vnfType)
						raise ClientError("Only one l3 address is supported on a port on VNF of type " + vnfType)
					for l3_address_id in port.addresses.l3:
						l3_address = port.addresses.l3[l3_address_id]
						"""
						if l3_address.configure.get_value() == "False" or l3_address.configure.get_value() == "false":
							LOG.error("Configure must be set to True on l3 address of VNF of type '%s'", vnfType)
							raise ClientError("Configure must be set to True on l3 address of VNF of type " + vnfType)
						"""
						unify_ip = l3_address.requested.get_as_text()

				mac = port.addresses.l2.get_value()
				port_id_nffg = int(port_id)
				port_list.append(Port(_id="port:"+str(port_id_nffg), unify_ip=unify_ip, mac=mac))
			if port.control.orchestrator.get_as_text() is not None:
				unify_env_variables.append("CFOR="+port.control.orchestrator.get_as_text())
			if port.metadata.length() > 0:
				LOG.error("Metadata are not supported inside a port element. Those should specified per node")
		if instance.metadata.length() > 0:
			for metadata_id in instance.metadata:
				metadata = instance.metadata[metadata_id]
				key = metadata.key.get_as_text()
				value = metadata.value.get_as_text()
				if key.startswith("variable:"):
					tmp = key.split(":",1)
					unify_env_variables.append(tmp[1]+"="+value)
				elif key.startswith("measure"):
					unify_monitoring = unify_monitoring + " " + value
				else:
					LOG.error("Unsupported metadata " + key)
					raise ClientError("Unsupported metadata " + key)
		if instance.resources.cpu.data is not None or instance.resources.mem.data is not None or instance.resources.storage.data is not None:
			LOG.warning("Resources are not supported inside a node element! Node: "+ instance.id.get_value())
		#the name of vnf must correspond to the type in supported_NFs
		if vnfType != "fake":
			vnf = VNF(_id = instance.id.get_value(), name = vnfType,vnf_template_location=vnfType, ports=port_list, unify_control=unify_control, unify_env_variables=unify_env_variables)
			nfinstances.append(vnf)
		else:
			fakevm = True
		LOG.debug("Required VNF: '%s'",vnfType)

	return nfinstances

def findEndPointId(domain, endpoint_name):
	endpoints = domain.ports

	for endpoint in endpoints:
		if endpoint_name in endpoint.name.get_value():
			return endpoint.id.get_value()
	LOG.error("Endpoint '%s' not found in current domain", endpoint_name)


def extractRules(content):
	'''
	Parses the message and translates the flowrules in the internal JSON representation
	Returns the rules and the endpoints in the internal format of the nffg_library
	'''

	LOG.debug("Extracting the flowrules to be installed in the controlled domain (eg. frog4 orchestrator)")

	try:
		tree = ET.ElementTree(ET.fromstring(content))
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ClientError("ParseError: %s" % e.message)

	infrastructure = Virtualizer.parse(root=tree.getroot())
	domain = infrastructure.nodes.node[constants.NODE_ID]
	flowtable = domain.flowtable
	instances = domain.NF_instances
	for instance in instances :
		first_id = instance.id.get_value()

	endpoints_dict = {}
	#Add the endpoint for the managment interface. This is a vlan type endpoint and the vlan id must correspond to the management vlan
	#assigned to the area where the Openstack of the jolnet deploy the vnf
	
	#endpoints_dict["mgmt"] = EndPoint(_id="mgmt", _type="vlan",vlan_id="tn-mgmt", interface="eth0", name="management", domain=endpoints_domain[3])

	flowrules = []
	'''
	#Add the rules for the management endpoint
	tmp_flowrule1 = FlowRule()
	tmp_flowrule1.id = "M1A"
	tmp_flowrule1.priority = 10
	tmp_match = Match()
	tmp_match.port_in = "endpoint:" + "mgmt"
	tmp_action=Action(output = "vnf:" +first_id + ":port:" + "0")
	tmp_flowrule1.match = tmp_match;
	tmp_flowrule1.actions.append(tmp_action)
	flowrules.append(tmp_flowrule1)

	tmp_flowrule2 = FlowRule()
	tmp_flowrule2.id = "M1B"
	tmp_flowrule2.priority = 10
	tmp_match = Match()
	tmp_match.port_in = "vnf:" + first_id +":port:" + "0"
	tmp_action=Action(output = "endpoint:" + "mgmt")
	tmp_flowrule2.match = tmp_match;
	tmp_flowrule2.actions.append(tmp_action)
	flowrules.append(tmp_flowrule2)
	'''
	for flowentry in flowtable:

		if flowentry.get_operation() is None:
			LOG.warning("Update of Flowrules is not supported by the UN! vnf: {0}".format(flowentry.id.get_value()))
			LOG.warning("This Flowrule will be disregarded by the Orchestrator if it already exists or created if it doesn't exist")
			#continue
			#LOG.error("Update of flowentry is not supported by the UN! flowentry: " + flowentry.id.get_value())
			#LOG.error("If you want to create a new flowentry please set \"operation=create\" to the flowentry element")
			#raise ClientError("Update of flowentry is not supported by the UN! vnf: "+flowentry.id.get_value())

		elif flowentry.get_operation() == 'delete':
			#This rule has to be removed from the graph
			continue

		elif flowentry.get_operation() != 'create':
			LOG.error("Unsupported operation for flowentry: " + flowentry.id.get_value())
			raise ClientError("Unsupported operation for flowentry: " + flowentry.id.get_value())


		flowrule = FlowRule()

		f_id = flowentry.id.get_value()
		priority = flowentry.priority.get_value()

		#Iterate on the match in order to translate it into the json syntax
		#supported by the frog4 orchestrator
		#match = {}
		match = Match()
		if flowentry.match is not None:
			if type(flowentry.match.get_value()) is str:
				#The tag <match> contains a sequence of matches separated by " " or ","
				#matches = flowentry.match.data.split(" ")
				matches = re.split(',| ', flowentry.match.data)
				for m in matches:
					tokens = m.split("=")
					elements = len(tokens)
					if elements != 2:
						LOG.error("Incorrect match "+flowentry.match.data)
						raise ClientError("Incorrect match")
					#The match is in the form "name=value"
					if not supportedMatch(tokens[0]):
						raise ClientError("Not supported match")

					#We have to convert the mdo match into the frog4 equivalent match

					setattr(match, equivalentMatch(tokens[0]), tokens[1])

			#We ignore the element in case it's not a string. It is possible that it is simply empty

		#The content of <port> must be added to the match
		#XXX: the following code is quite dirty, but it is a consequence of the nffg library

		portPath = flowentry.port.get_target().get_path()
		port = flowentry.port.get_target()
		tokens = portPath.split('/');

		if len(tokens) is not 6 and len(tokens) is not 8:
			LOG.error("Invalid port '%s' defined in a flowentry (len(tokens) returned %d)",portPath,len(tokens))
			raise ClientError("Invalid port defined in a flowentry")

		if tokens[4] == 'ports':
			#This is a port of the underlying domain. We have to extract the virtualized port name
			if port.name.get_value() not in physicalPortsVirtualization:
				LOG.error("Physical port "+ port.name.get_value()+" is not present in the UN")
				raise ClientError("Physical port "+ port.name.get_value()+" is not present in the UN")
			port_name = physicalPortsVirtualization[port.name.get_value()]
			port_id = findEndPointId(domain, port.name.get_value())
			LOG.debug("port name: %s", port.name.get_value())
			e_domain = endpoints_domain[int(port_id)]
                        e_vlanid = endpoints_vlanid[int(port_id)]
			if 'of' in port.name.get_value():
				tokens= port.name.get_value().split('/')
				LOG.debug("node: %s", tokens[0])
				LOG.debug("interface: %s", tokens[1])
				node_t = tokens[0]
				interface_t = tokens[1]
				#check the node id to understand the correct domain where the endpoint will be deployed
				if port_name not in endpoints_dict:
					LOG.debug("It's an onos_domain endpoint")
					endpoints_dict[port_name] = EndPoint(_id=str(port_id), _type="vlan",vlan_id=str(e_vlanid), interface=interface_t,name=port.name.get_value(), node_id=node_t, domain=e_domain)
					LOG.debug("%s", str(endpoints_dict[port_name]))
			else:
                                if port_name not in endpoints_dict:
					endpoints_dict[port_name] = EndPoint(_id=str(port_id), domain=e_domain, _type="vlan",vlan_id=str(e_vlanid), interface=str(port_id),name=port.name.get_value())
			match.port_in = "endpoint:" + endpoints_dict[port_name].id
		elif tokens[4] == 'NF_instances':
			#This is a port of the NF. I have to extract the port ID and the type of the NF.
			#XXX I'm using the port ID as name of the port
			vnf = port.get_parent().get_parent()
			vnf_id = vnf.id.get_value()
			port_id = int(port.id.get_value())
			match.port_in = "vnf:"+ vnf_id + ":port:" + str(port_id)
			# Check if this VNF port has L4 configuration. In this case rules cannot involve such port
			if domain.NF_instances[vnf_id].ports[port.id.get_value()].addresses.l4.get_value() is not None:
				LOG.error("It is not possibile to install flows related to a VNF port that has L4 configuration. Flowrule id: "+f_id)
				raise ClientError("It is not possibile to install flows related to a VNF port that has L4 configuration")
		else:
			LOG.error("Invalid port '%s' defined in a flowentry",port)
			raise ClientError("Invalid port "+port+" defined in a flowentry")

		if flowentry.action is not None:
			if type(flowentry.action.data) is str:
				#The tag <action> contains a sequence of actions separated by " " or ","
				#actions = flowentry.action.data.split(" ")
				actions = re.split(',| ', flowentry.action.data)

				for a in actions:
					action = Action()
					tokens = a.split(":")
					elements = len(tokens)
					if not supportedAction(tokens[0],elements-1):
						raise ClientError("action not supported")
					if elements == 1:
						setattr(action, equivalentAction(tokens[0]), True)
					else:
						setattr(action, equivalentAction(tokens[0]), tokens[1])
					flowrule.actions.append(action)

			# We ignore the element in case it's not a string. It could be simply empty.

		#The content of <out> must be added to the action
		#XXX: the following code is quite dirty, but it is a consequence of the nffg library

		portPath = flowentry.out.get_target().get_path()
		port = flowentry.out.get_target()
		tokens = portPath.split('/');
		if len(tokens) is not 6 and len(tokens) is not 8:
			LOG.error("Invalid port '%s' defined in a flowentry",portPath)
			raise ClientError("Invalid port "+portPath+" defined in a flowentry")

		if tokens[4] == 'ports':
			#This is a port of the underlying domain. I have to extract the ID
			#Then, I have to retrieve the virtualized port name, and from there
			#the real name of the port in the domain
			port_name = physicalPortsVirtualization[port.name.get_value()]
			port_id = findEndPointId(domain, port.name.get_value())
			LOG.debug("port name: %s - port id: %s", port.name.get_value(), str(port_id))
			e_domain = endpoints_domain[int(port_id)]
			e_vlanid = endpoints_vlanid[int(port_id)]
			LOG.debug("Domain: %s - vlanid: %s", e_domain, str(e_vlanid))
			
			if 'of' in port.name.get_value():
				tokens= port.name.get_value().split('/')
	                        LOG.debug("node: %s", tokens[0])
        	                LOG.debug("interface: %s", tokens[1])
                	        node_t = tokens[0]
                	        interface_t = tokens[1]
                                if port_name not in endpoints_dict:
                                        LOG.debug("It's an onos_domain endpoint")

                                        endpoints_dict[port_name] = EndPoint(_id=str(port_id),domain=e_domain, _type="vlan",vlan_id=e_vlanid, interface=interface_t, name=port.name.get_value(), node_id=node_t)
                                        LOG.debug(endpoints_dict[port_name].getDict(domain=True))
                        else:
                                if port_name not in endpoints_dict:
                                	endpoints_dict[port_name] = EndPoint(_id=str(port_id),domain=e_domain, _type="vlan",vlan_id=e_vlanid, interface=str(port_id),name=port.name.get_value())

			flowrule.actions.append(Action(output = "endpoint:" + endpoints_dict[port_name].id))
		elif tokens[4] == 'NF_instances':
			#This is a port of the NF. I have to extract the port ID and the type of the NF.
			#XXX I'm using the port ID as name of the port
			vnf = port.get_parent().get_parent()
			vnf_id = vnf.id.get_value()
			port_id = int(port.id.get_value())
			flowrule.actions.append(Action(output = "vnf:" + vnf_id + ":port:" + str(port_id)))

			# Check if this VNF port has L4 configuration. In this case rules cannot involve such port
			if domain.NF_instances[vnf_id].ports[port.id.get_value()].addresses.l4.get_value() is not None:
				LOG.error("It is not possibile to install flows related to a VNF port that has L4 configuration")
				raise ClientError("It is not possibile to install flows related to a VNF port that has L4 configuration")
		else:
			LOG.error("Invalid port '%s' defined in a flowentry",port)
			raise ClientError("Invalid port "+port+" defined in a flowentry")

		#Prepare the rule
		flowrule.id = f_id
		if priority is None:
			LOG.error("Flowrule '%s' must have a priority set", f_id)
			raise ClientError("Flowrule "+f_id+" must have a priority set")
		flowrule.priority = int(priority)
		flowrule.match = match

		flowrules.append(flowrule)

	LOG.debug("Rules extracted:")
	for rule in flowrules:
		LOG.debug(rule.getDict())

	return flowrules, endpoints_dict.values()


def supportedMatch(tag):
	'''
	Given an element within match, this function checks whether such an element is supported or node
	'''
	if tag in constants.supported_matches:
		LOG.debug("'%s' is supported!",tag)
		return True
	else:
		LOG.error("'%s' is not a supported match!",tag)
		return False

def equivalentMatch(tag):
	'''
	Given an element within match, this function return the element with equivalent meaning in native orchestrator NF-FG
	'''
	return constants.supported_matches[tag]

def supportedAction(tag,elements):
	'''
	Given an element within an action, this function checks whether such an element is supported or not
	'''
	if tag in constants.supported_actions:
		LOG.debug("'%s' is supported with %d elements!",tag,constants.supported_actions[tag])
		if constants.supported_actions[tag] == elements:
			return True
		else:
			LOG.debug("The action specifies has a wrong number of elements: %d",elements)
			return False
	else:
		LOG.error("'%s' is not a supported action!",tag)
		return False

def equivalentAction(tag):
	'''
	Given an element within action, this function return the element with equivalent meaning in native orchestrator NF-FG
	'''
	return constants.equivalent_actions[tag]


def updateDomainStatus(newContent):
	'''
	Read the status of the domain, and applies the required modifications to
	the NF instances and to the flowtable
	'''

	LOG.debug("Updating the file containing the status of the domain...")

	LOG.debug("Reading file '%s', which contains the current status of the domain...",constants.GRAPH_XML_FILE)
	try:
		oldTree = ET.parse(constants.GRAPH_XML_FILE)
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ServerError("ParseError: %s" % e.message)
	LOG.debug("File correctly read")

	infrastructure = Virtualizer.parse(root=oldTree.getroot())
	domain = infrastructure.nodes.node[constants.NODE_ID]
	flowtable = domain.flowtable
	nfInstances = domain.NF_instances


	#LOG.debug("Getting the new flowrules to be sent to the frog4 orchestrator")
	try:
		newTree = ET.ElementTree(ET.fromstring(newContent))
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ServerError("ParseError: %s" % e.message)

	newInfrastructure = Virtualizer.parse(root=newTree.getroot())
	newFlowtable = newInfrastructure.nodes.node[constants.NODE_ID].flowtable
	newNfInstances = newInfrastructure.nodes.node[constants.NODE_ID].NF_instances

	#Update the NF instances with the new NFs
	for instance in newNfInstances:
		if instance.get_operation() == 'delete':
			nfInstances[instance.id.get_value()].delete()
		else:
			for port_id in instance.ports.port:
				port = instance.ports[port_id]
				# Check if there is a request of a l3 address. If this is the case, then provide the response
				if port.addresses.l3.length() != 0:
					for l3_address_id in port.addresses.l3:
						l3_address = port.addresses.l3[l3_address_id]
						l3_address.provided.set_value(l3_address.requested.get_as_text())

				# Check if there is a request of l4 configuration. If this is the case, then provide the response
				l4_addresses = port.addresses.l4.get_value()
				if l4_addresses is not None:
					l4_response = {}
					# unify_port_mapping is in the form NF_id:port_id/protocol/port
					for k,v in unify_port_mapping.iteritems():
						tmp1 = k.split("/", 1)
						tmp2 = tmp1[0].split(":")
						if instance.id.get_value() == tmp2[0] and port_id == tmp2[1]:
							l4_response[tmp1[1]] = v
					port.addresses.l4.set_value(l4_response)
			instance.set_operation(None)
			nfInstances.add(instance)

	#Update the flowtable with the new flowentries
	for flowentry in newFlowtable:
		if flowentry.get_operation() == 'delete':
			flowtable[flowentry.id.get_value()].delete()
		else:
			flowentry.set_operation(None)
			flowtable.add(flowentry)
	#It is not necessary to remove conflicts, since they are already handled by the library,
	#i.e., it does not insert two identical rules

	try:
		tmpFile = open(constants.GRAPH_XML_FILE, "w")
		tmpFile.write(infrastructure.xml())
		tmpFile.close()
	except IOError as e:
		print "I/O error({0}): {1}".format(e.errno, e.strerror)
		LOG.error('I/O error')
		raise ServerError("I/O error")

	return infrastructure.xml()


'''
	Methods used to interact with the orchestrator
'''
def sendToOrchestrator(rules, vnfs, endpoints):
	'''
	Send rules and VNFs on the orchestrator
	'''
	LOG.info("Sending the new graph to the orchestrator (%s)",OrchestratorURL)
	global corr_graphids

	nffg = NF_FG()
	nffg.id = graph_id
	nffg.name = graph_name
	if unify_monitoring != "":
		nffg.unify_monitoring = unify_monitoring
	nffg.flow_rules = rules
	nffg.vnfs = vnfs
	nffg.end_points = endpoints

	#Delete endpoints that are not involved in any flowrule
	for endpoint in nffg.end_points[:]:
		if not nffg.getFlowRulesSendingTrafficToEndPoint(endpoint.id) and not nffg.getFlowRulesSendingTrafficFromEndPoint(endpoint.id):
			nffg.end_points.remove(endpoint)

	graph_url = OrchestratorURL + "/NF-FG/"

	try:
		# if the nffg translated in the nffg_library version has no flowrules, no vnfs and no endpoints, it's a delete request
		# note that the -2 is because mdo2frog4 inserts automatically the management endpoint for the jolnet environment
		if len(nffg.flow_rules) - 2 + len(nffg.vnfs) + len(nffg.end_points) <= 1:
			LOG.debug("No elements have to be sent to orchestrator...sending a delete request")
			LOG.debug("DELETE url: %s %s", graph_url, nffg.id)
			if debug_mode is False:
				if authentication is True and token is None:
					getToken()
				url = graph_url + corr_graphids[nffg.id]
				LOG.debug("url for delete: " + url)
				responseFromFrog = requests.delete(url, headers=headers)
				LOG.debug("Status code received from the orchestrator: %s",responseFromFrog.status_code)
				if responseFromFrog.status_code == 200:
					LOG.info("Graph successfully deleted")
				elif responseFromFrog.status_code == 401 or responseFromFrog.status_code == 404:
					LOG.info("Graph successfully deleted") #TODO: da aggiornare con l'id del grafo restituito dalla put
					#LOG.debug("Token expired, getting a new one...")
					#getToken()
					#newresponseFromFrog = requests.delete(url, headers=headers)
					#LOG.debug("Status code received from the Frog orchestrator: %s",newresponseFromFrog.status_code)
					#if newresponseFromFrog.status_code == 200:
					#	LOG.info("Graph successfully deleted")
					#else:
					#	LOG.error("Something went wrong while deleting the graph on the Frog4 orchestrator")
					#	raise ServerError("Something went wrong while deleting the graph on the Frog4 orchestrator")
				else:
					LOG.error("Something went wrong while deleting the graph on the orchestrator")
					raise ServerError("Something went wrong while deleting the graph on the orchestrator")
		else:
			LOG.debug("Graph that is going to be sent to the orchestrator:")
			LOG.debug("%s",nffg.getJSON(domain=True))
			LOG.debug("POST url: "+ graph_url)

			if debug_mode is False:

				if authentication is True and token is None:
					getToken()

				responseFromFrog = requests.put(graph_url, data=nffg.getJSON(domain=True), headers=headers)
				LOG.debug("Status code received from the orchestrator: %s",responseFromFrog.status_code)

				if responseFromFrog.status_code == 201:
					LOG.info("New VNFs and flows properly deployed")
					received_id = responseFromFrog.text
					LOG.info("Graph_id = %s  Received graph_id = %s",graph_id, received_id)
					corr_graphids[graph_id] = received_id
					LOG.info("Correspondance : %s -- %s", graph_id, corr_graphids[graph_id])
				elif responseFromFrog.status_code == 401:
					LOG.debug("Token expired, getting a new one...")
					getToken()
					newresponseFromFrog = requests.put(graph_url, data=nffg.getJSON(), headers=headers)
					LOG.debug("Status code received from the orchestrator: %s",newresponseFromFrog.status_code)
					if newresponseFromFrog.status_code == 201:
						LOG.info("New VNFs and flows properly deployed on the orchestrator")
					else:
						LOG.error("Something went wrong while deploying the new VNFs and flows on orchestrator")
						raise ServerError("Something went wrong while deploying the new VNFs and flows on the orchestrator")
				else:
					LOG.error("Something went wrong while deploying the new VNFs and flows on the orchestrator")
					raise ServerError("Something went wrong while deploying the new VNFs and flows on the orchestrator")

	except (requests.ConnectionError):
		LOG.error("Cannot contact the orchestrator at '%s'",graph_url + nffg.id)
		raise ServerError("Cannot contact the orchestrator at "+graph_url)

def getToken():
	global token, headers
	headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
	authenticationData = {'username': username, 'password': password}
	authentication_url = OrchestratorURL + "/login"
	resp = requests.post(authentication_url, data=json.dumps(authenticationData), headers=headers)
	try:
		resp.raise_for_status()
		LOG.debug("Authentication successfully performed")
		token = resp.text
		headers = {'Content-Type': 'application/json', 'X-Auth-Token': token}
	except HTTPError as err:
		LOG.error(err)
		raise ServerError("login failed: " + str(err))

'''
	Methods used in the initialization phase of the mdo2frog4
'''

def mdo2frog4Init():
	'''
	The mdo2frog4 maintains the state of the node in a tmp file.
	This function initializes such a file.
	'''
	if not setLog():
		return False

	LOG.info("Initializing the mdo2frog4...")

	if not readConfigurationFile():
		return False

	v = Virtualizer(id=constants.INFRASTRUCTURE_ID, name=constants.INFRASTRUCTURE_NAME)
	v.nodes.add(
		Infra_node(
			id=constants.NODE_ID,
			name=constants.NODE_NAME,
			type=constants.NODE_TYPE,
			resources=Software_resource(
				cpu=cpu,
				mem=memory,
				storage=storage
			)
		)
	)

	#Read information related to the infrastructure and add it to the
	#mdo2frog4 representation
	LOG.debug("Reading file '%s'...",infrastructureFile)
	try:
		tree = ET.parse(infrastructureFile)
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		return False
	root = tree.getroot()

	domain = v.nodes.node[constants.NODE_ID]

	#Read information related to the physical ports and add it to the
	#mdo2frog4 representation

	#global physicalPortsVirtualization
	global endpoints_domain, endpoints_vlanid

	ports = root.find('ports')
	portID = 1
	for port in ports:
		virtualized = port.find('virtualized')
		port_description = virtualized.attrib
		vlanid = port.find('vlanid').text
		LOG.debug("physicl name: %s - virtualized name: %s - type: %s - sap: %s - endpoint domain: %s - vlanid: %s", port.attrib['name'], port_description['as'],port_description['port-type'],port_description['sap'], port_description['endpoint_domain'], vlanid)
		endpoints_domain[portID] = port_description['endpoint_domain']
		endpoints_vlanid[portID] = vlanid
		LOG.debug('Double check: d: %s , vid: %s', endpoints_domain[portID], endpoints_vlanid[portID])

		physicalPortsVirtualization[port_description['as']] =  port.attrib['name']

		portObject = Virt_Port(id=str(portID), name=port_description['as'], port_type=port_description['port-type'], sap=port_description['sap'])
		domain.ports.add(portObject)
		portID = portID + 1

	#Save the mdo2frog4 representation on a file
	try:
		tmpFile = open(constants.GRAPH_XML_FILE, "w")
		tmpFile.write(v.xml())
		tmpFile.close()
	except IOError as e:
		print "I/O error({0}): {1}".format(e.errno, e.strerror)
		LOG.error('I/O error')
		return False

	LOG.info("The mdo2frog4 has been initialized")
	return True

def readConfigurationFile():
	'''
	Read the configuration file of the mdo2frog4
	'''

	global OrchestratorIP
	global OrchestratorURL
	global debug_mode
	global infrastructureFile
	global cpu, memory, storage
	global authentication, username, password, tenant
	global headers

	LOG.info("Reading configuration file: '%s'",constants.CONFIGURATION_FILE)
	config = ConfigParser.ConfigParser()
	config.read(constants.CONFIGURATION_FILE)
	sections = config.sections()

	if 'connections' not in sections:
		LOG.error("Wrong file '%s'. It does not include the section 'connections' :(",constants.CONFIGURATION_FILE)
		return False
	try:
		OrchestratorIP = config.get("connections","Frog4OrchestratorAddress")
		OrchestratorURL = OrchestratorURL + OrchestratorIP + ":" + config.get("connections","Frog4OrchestratorPort")
	except:
		LOG.error("Option 'Frog4OrchestratorAddress' or option 'Frog4OrchestratorPort' not found in section 'connections' of file '%s'",constants.CONFIGURATION_FILE)
		return False

	if 'orchestrator authentication' not in sections:
		LOG.error("Wrong file '%s'. It does not include the section 'orchestrator authentication' :(",constants.CONFIGURATION_FILE)
		return False
	try:
		authentication = config.getboolean("orchestrator authentication","authentication")
	except:
		LOG.error("Option 'authentication' not found in section 'orchestrator authentication' of file '%s'",constants.CONFIGURATION_FILE)
		return False
	if authentication is True:
		try:
			username = config.get("orchestrator authentication","username")
			password = config.get("orchestrator authentication","password")
		except:
			LOG.error("Option 'username' or 'password' not found in section 'orchestrator authentication' of file '%s'",constants.CONFIGURATION_FILE)
			return False
	if 'resources' not in sections:
		LOG.error("Wrong file '%s'. It does not include the section 'resources' :(",constants.CONFIGURATION_FILE)
		return False
	try:
		cpu = config.get("resources","cpu")
		memory = config.get("resources","memory")
		storage = config.get("resources","storage")
	except:
		LOG.error("Option 'cpu' or 'memory' or 'storage' not found in section 'resources' of file '%s'",constants.CONFIGURATION_FILE)
		return False
	
	if 'configuration' not in sections:
		LOG.error("Wrong file '%s'. It does not include the section 'configuration' :(",constants.CONFIGURATION_FILE)
		return False
	try:
		debug_mode_tmp = config.get("configuration", "debug_mode")
		if debug_mode_tmp == 'true':
			debug_mode = True
			LOG.info("Debug mode setted to True")

		else:
			debug_mode = False
			LOG.info("Debug mode setted to False")

        except:
		LOG.error("Option 'debug_mode' not found in section 'configuration' of file '%s'",constants.CONFIGURATION_FILE)
	try:
		infrastructureFile = config.get("configuration","PortFile")
	except:
		LOG.error("Option 'PortFile' not found in section 'configuration' of file '%s'",constants.CONFIGURATION_FILE)
		return False

	LOG.debug("CPU: %s", cpu)
	LOG.debug("memory: %s", memory)
	LOG.debug("storage: %s", storage)

	LOG.info("Url used to contact the orchestrator: %s",OrchestratorURL)

	return True

def adjustEscapeNffg(content):

	#.tmp.xml is the adjusted nffg coming from escape (output)
	#.txt.xml is the incoming nffg from escape (input)
	#info.xml contain the information to add to escape nffg (sap)

	LOG.debug("Adjusting the Escape Nffg")
	with open(".txt.xml", "w") as text_file:
		text_file.write("%s" % content)

	with open(".tmp.xml", 'w') as outfile:
		with open(".txt.xml", 'r') as infile_nffg:
			rowIter = iter(infile_nffg)
			for row in rowIter:
				outfile.write("%s" % row)
				if row.lstrip(' \t').startswith('<id>UUID11'):
					with open("port_info.xml", 'r') as infile:
						rowIter1= iter(infile)
						for row1 in rowIter1:
							outfile.write("%s" % row1)

	with open(".tmp.xml", 'r') as infile:
		#return infile.read()
		newContent = infile.read().replace(';',',')

	try:
		tree = ET.ElementTree(ET.fromstring(newContent))
	except ET.ParseError as e:
		print('ParseError: %s' % e.message)
		LOG.error('ParseError: %s', e.message)
		raise ClientError("ParseError: %s" % e.message)

	infrastructure = Virtualizer.parse(root=tree.getroot())
	domain = infrastructure.nodes.node[constants.NODE_ID]
	flowtable = domain.flowtable
	for flowentry in flowtable:
		if flowentry.get_operation() != "delete" and flowentry.get_operation() != "create":
			flowtable.remove(flowentry)
	for flowentry in flowtable:
		if flowentry.action is not None:
			flowentry.action = None
		if flowentry.match is not None:
			if type(flowentry.match.get_value()) is str:
				adjusted_matches = ""
                                matches = re.split(',| ', flowentry.match.data)
                                for m in matches:
                                	tokens = m.split("=")
                                        elements = len(tokens)
                                        if elements != 2:
                                        	LOG.error("Incorrect match "+flowentry.match.data)
                                                raise ClientError("Incorrect match")
                                        #The match is in the form "name=value"
                                        if not supportedMatch(tokens[0]):
                                        	raise ClientError("Not supported match")
                                        if tokens[0] == "dl_tag" or tokens[0] == "push_tag" or tokens[0] == "pop_tag":
											LOG.debug("Match deleted")
                                        else:
						if adjusted_matches == "":
							adjusted_matches = adjusted_matches +  m
						else:
                                        		adjusted_matches = adjusted_matches + ',' +  m
				flowentry.match.data = adjusted_matches
                                LOG.debug(adjusted_matches)

	return infrastructure.xml()

def loadTemplates():

	with open("tmp", 'w') as outfile:
		with open(constants.GRAPH_XML_FILE, 'r') as infile:
			rowIter = iter(infile)
        		for row in rowIter:
					if row.lstrip(' \t').startswith('</resources>'):
						outfile.write("\t\t\t</resources>\n")
						outfile.write("\t\t\t<capabilities>\n")
						outfile.write("\t\t\t\t<supported_NFs>\n")
						with open("template.xml", 'r') as template:
							rowIterT = iter(template)
							for rowT in rowIterT:
								outfile.write("\t\t\t\t\t %s" % rowT)

						outfile.write("\t\t\t\t</supported_NFs>\n")
						outfile.write("\t\t\t</capabilities>\n")
					else:
						outfile.write("%s" % row)

	os.remove(constants.GRAPH_XML_FILE)
	os.rename("tmp", constants.GRAPH_XML_FILE)

def setLog():
	config = ConfigParser.ConfigParser()
	config.read(constants.CONFIGURATION_FILE)
	sections = config.sections()

	if 'configuration' not in sections:
		return False

	try:
		LogFile = config.get("configuration","LogFile")
	except:
		return False

	try:
		LogLevel = config.get("configuration","LogLevel")
		log_level_tmp = LOG.DEBUG
		if LogLevel == 'info':
			log_level_tmp = LOG.INFO
		if LogLevel == 'warning':
			log_level_tmp = LOG.WARNING
		if LogLevel == 'error':
			log_level_tmp = LOG.ERROR
		if LogLevel == 'critical':
			log_level_tmp = LOG.CRITICAL
	except:
		return False

	log_level = log_level_tmp
	log_format = '%(asctime)s %(levelname)s %(message)s - %(filename)s'
	LOG.basicConfig(filename=LogFile, level=log_level, format=log_format, datefmt='%m/%d/%Y %I:%M:%S %p')
	return True


def connectEndpoints(flowrules):
	newFlowRules=[]
	endpoints=[]
	for rule in flowrules:
		if not rule.id.startswith("M"):
			if rule.match.port_in.startswith("endpoint"):
				endpoints.append(rule.match.port_in)
	for rule in flowrules:
		if not rule.id.startswith("M"):
			if rule.match.port_in.startswith("endpoint"):
				for endpoint in endpoints:
					if endpoint != rule.match.port_in:
							newAction = Action(output = endpoint)
							LOG.debug( endpoint)
							rule.actions = []
							rule.actions.append(newAction)
							newFlowRules.append(rule)
	LOG.debug("New rules generated:")
	for rule in newFlowRules:
		LOG.debug(rule.getDict())
	return newFlowRules

'''
	The following code is executed by guicorn at the boot of the mdo2frog4
'''

api = falcon.API()

#Global variables
OrchestratorURL = "http://"
OrchestratorIP = ""
infrastructureFile = ""
physicalPortsVirtualization = {}
graph_id = "default1"
graph_name = "NF-FG"
tcp_port = 10000
unify_port_mapping = OrderedDict()
unify_monitoring = ""
cpu = ""
memory = ""
storage = ""
authentication = False
username = ""
password = ""
tenant = ""
token = None
headers = {}
endpoint_vlanid = []
corr_graphids = {}
fakevm = False
debug_mode = False
endpoints_domain = {}
endpoints_vlanid = {}

if not mdo2frog4Init():
	LOG.error("Failed to start up the mdo2frog4.")
	LOG.error("Please, press 'ctrl+c' and restart the mdo2frog4.")

loadTemplates()
api.add_route('/virt',DoUsage())
api.add_route('/virt/ping',DoPing())
api.add_route('/virt/get-config',DoGetConfig())
api.add_route('/virt/edit-config',DoEditConfig())

