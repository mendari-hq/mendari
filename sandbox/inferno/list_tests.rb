# SPDX-License-Identifier: Apache-2.0
# The tests a run executes, as JSON on stdout: each test's id, its short id (as Inferno shows it,
# e.g. 3.6.01), its title and the requirement ids the kit says it verifies. Inferno's own results
# name tests by id only; scripts/inferno_check.py reads this file beside them, to show short ids
# and to fill in requirement ids when the expected results are written.
#
#   bundle exec ruby /opt/mendari/list_tests.rb dtr_payer_server_v220 1 3 4
require 'json'

ENV['NO_DB'] = 'true'
require 'inferno'
Inferno::Application.start(:suites)

suite_id, *group_short_ids = ARGV
suite = Inferno::Repositories::TestSuites.new.find(suite_id)
abort "no test suite #{suite_id}" if suite.nil?

def tests_under(runnable)
  runnable.children.flat_map do |child|
    child < Inferno::Entities::Test ? [child] : tests_under(child)
  end
end

groups = suite.groups
unless group_short_ids.empty?
  groups = group_short_ids.map do |short_id|
    groups.find { |group| group.short_id == short_id } || abort("no group #{short_id} in #{suite_id}")
  end
end

listed = groups.flat_map { |group| tests_under(group) }.map do |test|
  {
    id: test.id,
    short_id: test.short_id,
    title: test.title,
    requirements: test.verifies_requirements
  }
end
puts JSON.pretty_generate({ suite: suite_id, groups: groups.map(&:short_id), tests: listed })
