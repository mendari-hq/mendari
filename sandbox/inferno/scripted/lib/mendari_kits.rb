# SPDX-License-Identifier: Apache-2.0
# The kits this image carries beside the CRD kit, whose own lib/davinci_crd_test_kit.rb loads
# CRD (and, through it, SMART, TLS and US Core). Inferno loads only lib/*.rb in the working
# directory, in name order, so a gem in the Gemfile registers no suite until a file here
# requires it.
require 'bulk_data_test_kit'
