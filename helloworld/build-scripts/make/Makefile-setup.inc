
#
# We define a number of common variables that any included Makefile.angel files can use.
# In general, you should ONLY use variables whose name start with ANGEL in your own Makefiles.
#
# Most of the time, you'll want to append a new dependency to ANGEL_BUILD_TARGETS. Take a look at
# the top level Makefile.angel file for an example of how this is done.
#
# There are a handful of variables for paths, etc that are useful; review the comments below for details.
# Those variables that are exported are safe to use in Makefiles that are triggered from our dependencies.
# (This is commonly done inside service src dirs.)
#




### Define common variables:

# ANGEL_BUILD_TARGETS: append any targets that should be called when build and run-related targets are called
ANGEL_BUILD_TARGETS+=

# ANGEL_CLEAN_TARGETS: append any targets that should be called when 'make clean' is called
ANGEL_CLEAN_TARGETS+=

# ANGEL_TEST_TARGETS: append any targets that should be called during testing
ANGEL_TEST_TARGETS+=

# ANGEL_CLEAN_FILES: append any files that should be deleted when 'make clean' is called
ANGEL_CLEAN_FILES+=

# ANGEL_BASE_DIR: base path to base of our code dir
ANGEL_BASE_DIR:=$(abspath .)

# ANGEL_BUILT_DIRNAME: subdir path, relative to BASE_DIR, that built files are placed into
ANGEL_BUILT_DIRNAME:=built

# ANGEL_BUILT_BASEDIR: base path to directory we build code into that's shipped to prod
ANGEL_BUILT_BASEDIR:=$(ANGEL_BASE_DIR)/$(ANGEL_BUILT_DIRNAME)

# ANGEL_BUILD_SCRIPTS_DIR: path to dir that has build-related support files
ANGEL_BUILD_SCRIPTS_DIR:=$(ANGEL_BASE_DIR)/build-scripts

# ANGEL_BUILD_SCRIPTS_BIN_DIR: path to bin dir for build-related
ANGEL_BUILD_SCRIPTS_BIN_DIR:=$(ANGEL_BUILD_SCRIPTS_DIR)/bin

# ANGEL_PYTHON: path to default version of python, with flags, that you should use:
# (We no longer run python in -O mode: virtually all of the .pyc and .pyo files are identical)
# (Uses "=", not ":=", so that we get correct path after installing python via brew in venv builds.)
ANGEL_PYTHON=$(shell PATH=/usr/local/bin:$$PATH which python)

# ANGEL_UNAME: Darwin or Linux, useful for ifeq conditions in targets
ANGEL_UNAME:=$(shell uname)




### Makefile setup stuff that we need:

# We need secondary expansion to handle cases where out global vars are +='ed to after they're referenced:
.SECONDEXPANSION:


# Include our bin dir and python share dir in paths, so that Makefiles don't need full paths to the commands:
# (Set SHELL so that PATH becomes visible to recipes, otherwise an executable in one of these paths is only usable
# in a sub-shell. I.e. ./bin/x can't otherwise be used as "x", it would have to be used as "bash -c 'x'".)
SHELL:=/bin/bash
export PATH:=$(ANGEL_BUILD_SCRIPTS_BIN_DIR):$(ANGEL_BUILD_SCRIPTS_DIR)/built/bin:$(ANGEL_BASE_DIR)/bin:${PATH}
export PYTHONPATH:=$(ANGEL_BASE_DIR)/share/python:${PYTHONPATH}


# Define a clean-files target for ANGEL_CLEAN_FILES:
# (Removing files is so common that we have a special "ANGEL_CLEAN_FILES" target for it.)
angel-clean-files-helper:
	-rm -rf $(ANGEL_CLEAN_FILES)
ANGEL_CLEAN_TARGETS+=angel-clean-files-helper


# Remove some common files during 'make clean':
ANGEL_CLEAN_FILES+=$(ANGEL_BUILT_BASEDIR)
ANGEL_CLEAN_FILES+=$(TMPDIR)/chill-simplecache-*


# Make sure we're not running as root:
# (We use WITHOUT_ROOT_CHECK because we need to skip this check in debuild because fakeroot causes uid to "be" 0.)
ifeq ("$(shell id -u)", "0${WITHOUT_ROOT_CHECK}")
This will cause make to fail. This line isn't seen unless the uid is 0, and it will cause a make compile error.
endif




### Define a few useful functions:

# $(call angel-get-cwd): returns the current working directory of the Makefile that you call it from
angel-get-cwd=$(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# $(call angel-recursive-wildcard, <dir path>, <pattern>): returns a list of all files under dir-path that match pattern
angel-recursive-wildcard=$(foreach d,$(wildcard $1*),$(call angel-recursive-wildcard,$d/,$2) $(filter $(subst *,%,$2),$d))

