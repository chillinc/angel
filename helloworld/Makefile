
# Our Makefile manages dependencies for developer installs and
# builds release packages for server installations.
#
# Most likely, you'll only want to call one of the following:
#
#    make                 Install and check all dependencies. (Safe to call at anytime.)
#    make clean           Remove build files so that 'make' will rebuild stuff.
#                         (Don't call when services are running.)
#    make clean-all       Remove ALL build files and reset your repo to be as close as possible to a fresh checkout.
#    make test            Test that services and code pass tests.
#
#  Jenkins will probably want to call:
#    make jenkins-build-to-git        Generate a built version of the code, with checksums
#
# If you are needing to change the way the build system runs (e.g. adding dependencies),
# see ./build-scripts/make/Makefile-setup.inc for instructions on how to do that.
#



# Start by defining common variables used throughout our make files:
include build-scripts/make/Makefile-setup.inc



# Include Makefile.angel files that add targets to our main "build", "clean", and "test" targets:
include Makefile.angel */Makefile.angel



# Build all dependencies and code.
#  - Targets are added to ANGEL_BUILD_TARGETS in included Makefile.angel files.
#  - This is called by the main script when it is run from inside a git checkout.
#  - This allows us to put changes into the Makefile without devs knowing they have to run make.
#  - This also means we have to be able to skip over rebuilding any of the listed files if they already exist,
#    so calls to this need to return almost instantly and silently when there's nothing to be done.
#  - Also, keep in mind that the main script issues a lock call around this to avoid concurrent runs;
#    so don't just call this outside blindly otherwise concurrent runs of Make can break things.
.PHONY: build
.DEFAULT_GOAL:=build
build: $$(ANGEL_BUILD_TARGETS)
	@# Nothing to do here; everything is in dependencies.



# Verify that services and code run correctly.
#  - Targets are added to ANGEL_TEST_TARGETS in included Makefile.angel files.
#  - Depend on test-for-untracked-files first because it's common enough and will halt everything before we even start.
#  - Wrap the real test logic so that we can skip tests when our get-code-sha1 matches prior test runs.
#  - Don't call get-code-sha1 in a variable; it'll slow down all other targets that don't need it.
#  - Don't store the value of get-code-sha1 -- we want to see if it somehow changed after running the tests.
#  - Use files under /tmp so that Jenkins or other setups with multiple checkouts can see cross-checkout build success.
.PHONY: test
test: test-for-untracked-files clean-python-stale-pyc
	@mkdir -p .build_tests
	@if [ -e $$TMPDIR/.build_test_$(shell ./build-scripts/bin/chill-build-get-code-sha1) ]; then \
		touch .build_tests/.build_test_$(shell ./build-scripts/bin/chill-build-get-code-sha1); fi
	@if [ ! -e .build_tests/.build_test_$(shell ./build-scripts/bin/chill-build-get-code-sha1) ]; then \
		time $(MAKE) $(MFLAGS) test-helper; fi
	@touch .build_tests/.build_test_$(shell ./build-scripts/bin/chill-build-get-code-sha1) \
		$$TMPDIR/.build_test_$(shell ./build-scripts/bin/chill-build-get-code-sha1)
.PHONY: test-helper
test-helper: $$(ANGEL_BUILD_TARGETS)
	@./bin/angel status --wait=15 > /dev/null || (echo "Error: services not ok." && ./bin/angel status && exit 1)
	time $(MAKE) $(MFLAGS) $(ANGEL_TEST_TARGETS)
	time $(ANGEL_BUILD_SCRIPTS_BIN_DIR)/angel-run-tests $(if ${WITHOUT_NETWORK},!uses-network) ${RUN_TESTS_FLAGS}
	@./bin/angel tool monitoring-sentry check-for-unhandled-exceptions



# A simple test for untracked files.
.PHONY: test-for-untracked-files
ANGEL_TEST_TARGETS+=test-for-untracked-files
test-for-untracked-files:
	@if [ "x" != "x`git status --porcelain`" ] ; then \
		echo "Untracked or uncommitted files in git checkout; bailing." && git status && exit 1 ; fi



# Install target isn't used, but it's a standard target so define it to avoid confusion.
install:
	@echo "Nothing to install (hint: see README.md)."



# Remove any files that are commonly rebuilt (use clean-all for full clean).
#  - Targets are added to ANGEL_CLEAN_TARGETS in included Makefile.angel files.
#  - We also have a ANGEL_CLEAN_FILES target for listing files directly.
clean: $$(ANGEL_CLEAN_TARGETS)
	@# Nothing to do here; everything is in dependencies.

# Remove .build_tests when cleaning:
ANGEL_CLEAN_FILES+=.build_tests


# Reset the entire checkout to a like-new state.
#  - Depend on test-for-untracked-files to prevent this from deleting uncommitted work.
clean-all: test-for-untracked-files
	$(MAKE) $(MFLAGS) clean
	git clean -d -f -x
	-rm -f $$TMPDIR/chill-build-cache-* /tmp/chill-build-cache-*
	-git fetch --prune


