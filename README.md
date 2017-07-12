# Mdo2frog4

The Mdo2frog4 is an intermediate module situated between the [https://github.com/netgroup-polito/frog4-orchestrator](FROG4 
global orchestrator) and the upper layers of the Unify architecture. It operates as follows:
  * receives commands from the upper layers of the Unify architecture based on the virtualizer 
    library defined by WP3;
  * converts those commands in the formalism natively supported by the FROG4 orchestrator
    (described in [https://github.com/netgroup-polito/nffg-library](https://github.com/netgroup-polito/nffg-library));
  * sends the command to the controlled domain (e.g., FROG orchestrator) through the API.

This module is an adaptation of a old virtualizer module used to integrate the un-orchestrator 
with the upper layes of the Unify architecture.
In this case the virtualizer(renamed in Mdo2frog4) interact through its northbound with the MultiDomain Orchestrator
(that is basically Escape) and through its southbound with the FROG4 global orchestrator.

## Required libraries

The Mdo2frog4 works only with python 2.7

	; Install required libraries
	; - libxml2-dev: nice library to parse and create xml
	
	$ sudo apt-get install libxml2-dev

	; Install other required libraries 
	$ sudo apt-get install python-pip
	$ sudo pip install gunicorn falcon cython requests

	; Install the virtualizer library
	; The virtualizer library has to be installed. After installing the python library, in the virtualizer_library directory 
	; there is a README with the instructions to install this library.

## Getting the code

To get the Mdo2frog4 through GIT:
	
	; Clone the code
	
	$ git clone https://github.com/netgroup-polito/mdo2frog4.git
	$ cd mdo2frog4
	$ git submodule init && git submodule update
	
## How to configure the Mdo2frog4

The Mdo2frog4 gets its configuration from the file [./config/configuration.ini](config/configuration.ini), 
which must be properly edited before starting the Mdo2frog4 itself.

Mdo2frog4 export an abstract view of the domain managed by the frog4 orchestrator. 
This abstract view must be configured before starting the Mdo2frog4 and the MdO:

	- In the file [./template.xml](template.xml) all VM images must be set with correct type and ports as showed in the file itself. 
	  In that way when MdO will get the abstract view of the domain, it will save which VMs can be launched and how many ports it can have. 

	- In the file [./port_info.xml](port_info.xml) all the ports of the controlled domain must be set with correct id, name, port-type and eventually sap.
	  In that way when MdO will get the abstract view of the domain, it will understand how many ports there are in the controlled domain and which these port are.

	- Every port that the mdo2frog4 must export through its northbound interface, must be also set in the file setted as PortFile in the 
	  configuration file [./config/configuration.ini](config/configuration.ini)(in the configuration.ini sample this file is called portDescription.ini).
	  [./config/portDescription.ini](config/portDescription.ini) uses the format described with the xml-schema [./config/portDescription-schema.xsd](portDescription-schema.xsd).
	  In this file must be set for each port, a generic port name, the name used in the underlying domain, the port type, the sap id, the domain
	  to which correspond and the vlan id, because these port will be translated as vlan endpoints.
	  
	The content of port_info.xml and template.xml is added to a temporary file that represents the actual status of the mdo2frog4 in the same format used by the MdO to build its xml graphs. 
	port_info.xml and template.xml are validated by virtualizerzer library.

The Mdo2frog4 stores the actual configuration and status of the domain in a automatically created file called .domainConfiguration.xml.
This file contains the underlying domain current view. At start time, it contains a representation of the domain calculated from the configuration files 
named above. When a NFFG is deployed succesfully, this file is updated with the new nf_instance and new flow rules.

## How to run the Mdo2frog4

	$ gunicorn -b ip:port mdo2frog4:api

where 'ip' and 'port' must be set to the desired values.


## Nffg file examples

Examples of nffg that can be sent to the Mdo2frog4 are available in [./nffg_examples](nffg_examples).
In particular:
  * [./nffg_examples/instantiateVM.xml](./nffg_examples/instantiateVM.xml):
    a simple graph that create an instance of a vm.
  * [./nffg_examples/deleteVM.xml](./nffg_examples/deleteVM.xml): 
    a graph that delete the vm instance.

## Rest API

The Mdo2frog4 accept commands through its REST interface. The main REST commands 
to be used to interact with it are detailed in the following.
The Mdo2frog4 uses the NETCONF protocol.

Get information about the Mdo2frog4

    GET /virt/ HTTP/1.1
    
Test the Mdo2frog4 aliveness

    GET /virt/ping HTTP/1.1

Retrieve the current configuration of the controlled domain

    POST /virt/get-config HTTP/1.1

This command returns the current configuration in the format defined by the virtualizer library

Deploy a new configuration 

    POST /virt/edit-config HTTP/1.1

The body of the message must contain the new configuration for the controlled domain 
espressed in the format defined by the Virtualizer library.

### Send commands to the Mdo2frog4
    
In order to interact with the Mdo2frog4 throug its REST API, you can use your favorite REST tool (e.g., some nice 
plugins for Mozilla Firefox). Just in also use the CURL command line tool, such as in the following example 
(where the NF-FG to be instantiated is stored in the file 'myGraph.xml'):

$ curl -i -d "@myGraph.json" -X POST  http://mdo2frog4-address:port/virt/edit-config
