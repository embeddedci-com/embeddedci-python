# Developer shortcuts. Unit tests need no hardware; the e2e targets drive a real BenchPod.
#
#   make test
#   make e2e POD=192.168.1.215 [FIRMWARE=path/to/scenario-sensors.elf] [USB=/dev/cu.usbmodem…] [REV=v3]
#   BENCHPOD_API_KEY=eci_… make e2e-cloud CLOUD_DEVICE=benchpod-v2.0.0
#
# The board's LA voltage is set once in packages/embeddedci/tests/conftest.py; the bench wiring in
# packages/embeddedci/tests/e2e/conftest.py (override with BENCHPOD_E2E_* variables).

PYTHON ?= python
POD ?=
FIRMWARE ?=
USB ?=
CLOUD_DEVICE ?=
REV ?=

FIRMWARE_OPT = $(if $(FIRMWARE),--benchpod-firmware=$(abspath $(FIRMWARE)),)
# REV=v3 makes a pod that reports another board revision fail instead of taking the v2 branches.
REV_ENV = $(if $(REV),BENCHPOD_E2E_BOARD_REV=$(REV),)

.PHONY: test e2e e2e-cloud check-sdk

test:  # one run per package, like CI (their tests/conftest.py modules share a name)
	$(PYTHON) -m pytest -q packages/embeddedci
	$(PYTHON) -m pytest -q packages/embeddedci-mcp
	$(PYTHON) -m pytest -q packages/embeddedci-openhtf

# The e2e tiers must test THIS checkout.  Installing a package that depends on embeddedci (the
# OpenHTF plug, say) can quietly swap the editable install for the PyPI release, and every test
# then exercises old code while the header still looks fine.
check-sdk:
	@$(PYTHON) scripts/check_sdk.py

e2e: check-sdk
	@test -n "$(POD)" || { echo "usage: make e2e POD=<pod host> [FIRMWARE=app.elf] [USB=/dev/…] [REV=v3]"; exit 2; }
	# tests/examples too: it is the first thing a new user runs, so it must not rot.
	$(REV_ENV) BENCHPOD_E2E_USB="$(USB)" $(PYTHON) -m pytest -v -rs \
		packages/embeddedci/tests/e2e packages/embeddedci/tests/examples \
		--benchpod-connection=$(POD) $(FIRMWARE_OPT)
	$(REV_ENV) $(PYTHON) -m pytest -v -rs packages/embeddedci-mcp/tests/test_e2e_mcp.py \
		--benchpod-connection=$(POD) $(FIRMWARE_OPT)
	$(PYTHON) -m pytest -v -rs packages/embeddedci-openhtf/tests/test_e2e_openhtf.py \
		--benchpod-connection=$(POD) $(FIRMWARE_OPT)

e2e-cloud:
	@test -n "$(CLOUD_DEVICE)" || { echo "usage: BENCHPOD_API_KEY=eci_… make e2e-cloud CLOUD_DEVICE=<name>"; exit 2; }
	BENCHPOD_E2E_CLOUD_DEVICE="$(CLOUD_DEVICE)" $(PYTHON) -m pytest -v -rs \
		packages/embeddedci/tests/e2e/test_e2e_cloud.py
