#!/bin/bash
set -e


# Pre-Activate prepares the system to run this version of our codebase
# by creating users and installing necessary packages.


# Check that we're running as root on Ubuntu, with ANGEL_VERSIONS_DIR set:
if [ ! -e /etc/lsb-release ] ; then
	echo "Skipping post install logic: not on ubuntu?"
	exit 0
fi
source /etc/lsb-release
if [ ! -e "$ANGEL_VERSIONS_DIR" ] ; then
	echo "Error: ANGEL_VERSIONS_DIR isn't set."
	exit 1
fi
if [[ $EUID -ne 0 ]] ; then
   echo "Error: not running as root" 1>&2
   exit 1
fi



# Chdir to a common dir, for path consistency:
SCRIPTS_DIR=`dirname $BASH_SOURCE`/scripts
cd $SCRIPTS_DIR



# Create helloworld user, dirs, and settings:
if [ "`id helloworld 2>/dev/null`" == "" ] ; then
	echo "Creating helloworld user"
	useradd helloworld -d /usr/lib/helloworld/.home --system --shell /bin/false
fi
if [ ! -e /mnt/helloworld ] ; then
	mkdir /mnt/helloworld
fi
chown helloworld.helloworld /mnt/helloworld
if [ ! -e /etc/helloworld/conf.d ] ; then
	mkdir -p /etc/helloworld/conf.d
	echo 'RUN_AS_USER="helloworld"' > /etc/helloworld/conf.d/helloworld.conf
	echo 'RUN_AS_GROUP="helloworld"' >> /etc/helloworld/conf.d/helloworld.conf
	echo 'CACHE_DIR="/mnt/helloworld/cache"' >> /etc/helloworld/conf.d/helloworld.conf
	echo 'DATA_DIR="/mnt/helloworld/data"' >> /etc/helloworld/conf.d/helloworld.conf
	echo 'LOCK_DIR="/var/run/lock/helloworld"' >> /etc/helloworld/conf.d/helloworld.conf
	echo 'LOG_DIR="/mnt/helloworld/logs"' >> /etc/helloworld/conf.d/helloworld.conf
	echo 'RUN_DIR="/mnt/helloworld/run"' >> /etc/helloworld/conf.d/helloworld.conf
	echo 'TMP_DIR="/mnt/helloworld/tmp"' >> /etc/helloworld/conf.d/helloworld.conf
fi



# Install Java 1.7:
# (We don't run our own repo, so we don't have a place to tuck this, so doing this here for now.)
JDK_MINOR_VERSION=67
if [ ! -e /opt/jdk1.7.0_$JDK_MINOR_VERSION ] ; then
	echo "Installing Java 1.7.0-$JDK_MINOR_VERSION"
	wget --user=helloworld --password=helloworld https://example.com/path-to-java-17_1.7.0.$JDK_MINOR_VERSION.1-1_amd64.deb
	dpkg -i java-17_1.7.0.$JDK_MINOR_VERSION.1-1_amd64.deb
	rm java-17_1.7.0.$JDK_MINOR_VERSION.1-1_amd64.deb
fi



# Install dependencies for running make and debian package install stuff:
if [ ! -e /usr/bin/make ] ; then DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none apt-get install -y make ; fi
if [ ! -e /usr/bin/mk-build-deps ] ; then DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none apt-get install -y devscripts ; fi
if [ ! -e /usr/bin/equivs-control ] ; then DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none apt-get install -y equivs ; fi



# Run mk-build-deps to install dependencies, using our own control file:
# (Only run mk-build-deps if the currently active version is different than the new version, to speed things up.)
if [ ! -e "$SCRIPTS_DIR/debian-$DISTRIB_CODENAME.control" ] ; then
	echo "Missing control file for Ubuntu $DISTRIB_CODENAME"
	exit 1
fi
if [ ! -e "$ANGEL_VERSIONS_DIR/_default/_default/.angel/scripts/debian-$DISTRIB_CODENAME.control" ] || [ "`md5sum $ANGEL_VERSIONS_DIR/_default/_default/.angel/scripts/debian-$DISTRIB_CODENAME.control | cut -b -32`" != "`md5sum $SCRIPTS_DIR/debian-$DISTRIB_CODENAME.control | cut -b -32`" ] ; then
	echo "Updating dependencies"

	# Update apt-get, first, in case repo endpoints have changed:
	apt-get update

	# Generate a package that triggers installation of build-time dependencies:
	DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none mk-build-deps --install $SCRIPTS_DIR/debian-$DISTRIB_CODENAME.control --tool 'apt-get -y'

	# We need to also install the generated package, as the mk-build-deps doesn't fail if some packages are missing:
	# (Also, this flags the dependencies as being required, so autoremove won't delete them.)
	dpkg -i helloworld-build-deps_1.0_all.deb
	rm helloworld-build-deps_1.0_all.deb
fi



# Stop services that Ubuntu "decided" we wanted running just because we installed them:
if [ -e /var/run/apache2/apache2.pid ] ; then service apache2 stop ; fi
if [ -e /etc/rc2.d/S91apache2 ] ; then sysv-rc-conf --level 2345 apache2 off ; fi
if [ -e /var/run/nagios/nrpe.pid ] ; then service nagios-nrpe-server stop ; fi
if [ -e /etc/rc2.d/S20nagios-nrpe-server ] ; then sysv-rc-conf --level 2345 nagios-nrpe-server off ; fi
if [ -e /var/run/nagios3/nagios3.pid ] ; then service nagios3 stop ; fi
if [ -e /etc/rc2.d/S30nagios3 ] ; then sysv-rc-conf --level 2345 nagios3 off ; fi
if [ -e /var/lib/ntop/ntop.pid ] ; then service ntop stop ; fi
if [ -e /etc/rc2.d/S20ntop ] ; then sysv-rc-conf --level 2345 ntop off ; fi

# Work around cassandra writeable homedir path issues:
if [ ! -e "/usr/lib/helloworld/.home" ] ; then
	# Pig shell fails to run if it can't write to ~helloworld/.pig_history.
	# Plus: https://issues.apache.org/jira/browse/CASSANDRA-6449
	mkdir -p /usr/lib/helloworld/.home
	chown helloworld.helloworld /usr/lib/helloworld/.home
	perl -pi -e 's:/usr/lib/helloworld:/usr/lib/helloworld/.home:gs' /etc/passwd
fi

if [ ! -e "/root/.cassandra" ] ; then
	mkdir -p /root/.cassandra
	echo "Created by helloworld pre_activate.sh to work around https://issues.apache.org/jira/browse/CASSANDRA-6449" > /root/.cassandra/readme.txt
fi

# Work around "stdin: is not a tty" bug (see https://bugs.launchpad.net/ubuntu/+source/xen-3.1/+bug/1167281):
perl -pi -e 's:^mesg n:tty -s && mesg n:gs' /root/.profile
