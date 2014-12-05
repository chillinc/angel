#!/bin/bash
set -e

if [ ! -e /usr/bin/helloworld ] ; then
	ln -fns /usr/lib/helloworld/_default/_default/bin/helloworld /usr/bin/helloworld
fi

/usr/bin/helloworld tool devops update-system-config