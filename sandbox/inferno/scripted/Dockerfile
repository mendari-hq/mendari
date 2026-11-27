# SPDX-License-Identifier: Apache-2.0
# The Inferno Da Vinci CRD test kit (Apache-2.0, inferno-framework/davinci-crd-test-kit) with the
# Bulk Data test kit beside it, built by Mendari for its scripted conformance runs (LP-23l):
# suites smart_client_stu2_2 (SMART 1.0.3, a CRD dependency) and bulk_data_v200_client, on
# inferno_core 1.6.0. The CRD kit's own suite, crd_server_v221, is in the image too; no job
# runs it. The kits publish no image and their own Dockerfiles pin nothing, so this one pins
# everything it takes:
# - the Ruby base image, by digest (the kits' Ruby, 3.3.6);
# - the CRD kit's source, fetched by its commit SHA and checked against it;
# - Bundler, downloaded and checked against its sha256 before it is installed;
# - inferno_core, the release by its sha256, with two pins moved to fixed releases (below);
# - every gem, from Mendari's Gemfile and Gemfile.lock (the kit's, with bulk_data_test_kit
#   0.14.0 added, Linux platforms only and a CHECKSUMS section): Bundler checks each gem against
#   its sha256 and refuses any change to the lock.
# A run's list of tests comes from its session (scripts/inferno_session.py), which knows the
# suite options and nested groups, so this image carries no list_tests.rb. To bump a kit, see
# sandbox/inferno/README.md ("Bumping the kit").
FROM ruby:3.3.6@sha256:347edd0c70ee08d87de9f01b99de2f14a64cedb5d1bfb38457dfe8cd0bf113c5

ARG KIT_REPOSITORY=https://github.com/inferno-framework/davinci-crd-test-kit.git
ARG KIT_COMMIT=86be7f4bf6817960aa204f8ce134cea466bd73d2
ARG BUNDLER_VERSION=2.7.2
ARG BUNDLER_SHA256=1decaf9e2e1acb91b6586a2925c8f3f6da2334a82731a62ff2ded1b83c283871

ENV APP_ENV=production \
    BUNDLE_FROZEN=true \
    BUNDLE_WITHOUT=development:test
WORKDIR /opt/inferno

RUN git init --quiet . \
 && git fetch --quiet --depth 1 "$KIT_REPOSITORY" "$KIT_COMMIT" \
 && git checkout --quiet FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$KIT_COMMIT" \
 && rm -rf .git

RUN curl -fsSL -o /tmp/bundler.gem "https://rubygems.org/downloads/bundler-${BUNDLER_VERSION}.gem" \
 && echo "${BUNDLER_SHA256}  /tmp/bundler.gem" | sha256sum -c - \
 && gem install --local --no-document /tmp/bundler.gem \
 && rm /tmp/bundler.gem

# inferno_core 1.6.0, the newest release, pins activesupport ~> 7.2.3.1 and sqlite3 ~> 1.4, and
# every release in those ranges has an open advisory (GHSA-8474-xrp6-wh69; GHSA-4fqr-96q8-xrfx,
# GHSA-mwm8-39rw-8826, GHSA-28hh-pr2h-2w89). So the image takes that release from rubygems.org,
# checked against its sha256, and moves those two pins, and nothing else, to the fixed releases
# (activesupport 8.0.5.2, sqlite3 2.10.2); the Gemfile names it by path. The build fails if the
# gemspec's lines are not the ones expected. Drop this once a release takes the fixed versions.
ARG INFERNO_CORE_VERSION=1.6.0
ARG INFERNO_CORE_SHA256=ab8796a54bb415643faeff891b8d29862886dd73bb68c117c34c32ae8095bcc9
RUN gem="/tmp/inferno_core-${INFERNO_CORE_VERSION}.gem" \
 && spec="/opt/vendor/inferno_core-${INFERNO_CORE_VERSION}/inferno_core.gemspec" \
 && curl -fsSL -o "$gem" "https://rubygems.org/downloads/inferno_core-${INFERNO_CORE_VERSION}.gem" \
 && echo "${INFERNO_CORE_SHA256}  $gem" | sha256sum -c - \
 && mkdir -p /opt/vendor \
 && gem unpack "$gem" --target /opt/vendor \
 && gem spec "$gem" --ruby > "$spec" \
 && grep -qF '%q<activesupport>.freeze, ["~> 7.2.3.1".freeze]' "$spec" \
 && grep -qF '%q<sqlite3>.freeze, ["~> 1.4".freeze]' "$spec" \
 && sed -i -E \
      -e 's/(%q<activesupport>\.freeze, \[)"~> 7\.2\.3\.1"\.freeze/\1">= 8.0.5.2".freeze, "< 8.1".freeze/' \
      -e 's/(%q<sqlite3>\.freeze, \[)"~> 1\.4"\.freeze/\1">= 2.10.2".freeze, "< 3".freeze/' "$spec" \
 && grep -qF '%q<activesupport>.freeze, [">= 8.0.5.2".freeze, "< 8.1".freeze]' "$spec" \
 && grep -qF '%q<sqlite3>.freeze, [">= 2.10.2".freeze, "< 3".freeze]' "$spec" \
 && rm "$gem"

# The kit's Gemfile with two lines added (bulk_data_test_kit, and inferno_core by path), and its
# lock.
COPY Gemfile ./Gemfile
COPY Gemfile.lock ./Gemfile.lock
RUN bundle install --jobs 4 && bundle exec ruby -e 'exit 0'

# Inferno loads each file in lib/ as it boots, in name order: the CRD kit's own
# (davinci_crd_test_kit.rb), then the Bulk kit (mendari_kits.rb), then the offline patch.
COPY lib/mendari_kits.rb lib/mendari_kits.rb
# Every FHIR validator in the kits runs without a terminology server (offline_terminology.rb).
COPY offline_terminology.rb lib/zz_mendari_offline_terminology.rb

RUN useradd --create-home --uid 10001 inferno \
 && mkdir -p data log tmp \
 && chown -R inferno data log tmp
USER inferno
# An empty session database (SQLite, in data/). The scripted compose project mounts one named
# volume there for the web app and its worker, which starts from this migrated database.
RUN bundle exec inferno migrate

EXPOSE 4567
CMD ["bundle", "exec", "puma"]
