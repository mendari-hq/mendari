# SPDX-License-Identifier: Apache-2.0
# Mendari's runs validate offline: every FHIR resource validator the kits define (their suites',
# groups' and tests') gets no terminology server, so nothing Inferno validates is sent to
# tx.fhir.org, the kits' default. The HL7 validator then reports codes it cannot check as
# warnings, never as errors. The Dockerfile copies this file into the kit's lib/ as
# zz_mendari_offline_terminology.rb, where Inferno loads it at boot, after the kits (Inferno
# loads lib/*.rb in name order; the requires below make the order explicit, whichever kits the
# image carries).
Dir.glob(File.join(__dir__, '*.rb')).sort.each do |path|
  require_relative path unless File.realpath(path) == File.realpath(__FILE__)
end

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
