angel
=====

Angel automates the installs, supervision, and upgrades of services. Key features include multi-version and multi-branch concurrent installs that allow for easy testing and roll-forward/roll-back; hard-linked, checksum-based deploy setup for quick upgrades with minimal memory pressure; and a broad set of scriptable tools for development and automation.

This code should be considered "alpha" quality. It is based on code in production, but as it's extracted out, will not necessarily be deployable in its current state. In addition to the python angel module, this repo contains a Makefile system that we use to build and deploy code. Angel itself does not build code. On a versioned setup, you add a version into angel by pointing at a directory and saying, "add this as version X", which will cause angel to import into its hardlink structure the files. I've found using a "binary" git repo of the compiled code extremely efficient at transmitting and installing upgrades of codebases, so the Makefiles under the helloworld example will generate those binary git repos (which then need to be checked out on the target install nodes).

This initial release is meant to provide a reference point for future work.
