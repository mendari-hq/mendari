# SPDX-License-Identifier: Apache-2.0
# Mendari's runs validate offline: every FHIR resource validator the kit defines (its suites',
# groups' and tests') gets no terminology server, so nothing Inferno validates is sent to
# tx.fhir.org, the kit's default. The HL7 validator then reports codes it cannot check as
# warnings, never as errors. The Dockerfile copies this file into the kit's lib/, where Inferno
# loads it at boot, after the kit (Inferno loads lib/*.rb in name order; the require below makes
# the order explicit).
require_relative 'davinci_dtr_test_kit'

offline = 0
[Inferno::Entities::TestSuite, Inferno::Entities::TestGroup, Inferno::Entities::Test].each do |kind|
  kind.descendants.each do |runnable|
    next unless runnable.respond_to?(:fhir_validators)

    runnable.fhir_validators.each_value do |validators|
      validators.each do |validator|
        next unless validator.is_a?(Inferno::DSL::FHIRResourceValidation::Validator)

        validator.validation_context { txServer nil }
        offline += 1
      end
    end
  end
end
warn "mendari: #{offline} FHIR validators run without a terminology server"
