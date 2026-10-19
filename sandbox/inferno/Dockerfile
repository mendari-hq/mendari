# SPDX-License-Identifier: Apache-2.0
# The Inferno Da Vinci DTR test kit (Apache-2.0, inferno-framework/davinci-dtr-test-kit), built
# by Mendari for its conformance runs (sandbox/inferno/README.md). The kit publishes no image and
# its own Dockerfile pins nothing, so this one pins everything it takes:
# - the Ruby base image, by digest (the kit's Ruby, 3.3.6);
# - the kit's source, fetched by its commit SHA and checked against it;
# - Bundler, downloaded and checked against its sha256 before it is installed;
# - every gem, from Mendari's Gemfile.lock (the kit's, with Linux platforms and a CHECKSUMS
#   section): Bundler checks each gem against its sha256 and refuses any change to the lock.
# To bump the kit, see sandbox/inferno/README.md ("Bumping the kit").
FROM ruby:3.3.6@sha256:347edd0c70ee08d87de9f01b99de2f14a64cedb5d1bfb38457dfe8cd0bf113c5

ARG KIT_REPOSITORY=https://github.com/inferno-framework/davinci-dtr-test-kit.git
ARG KIT_COMMIT=1b76dd3f565433a3157808da2d6d0c08a797b2f0
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

COPY Gemfile.lock ./Gemfile.lock
RUN bundle install --jobs 4 && bundle exec ruby -e 'exit 0'

# Every FHIR validator in the kit runs without a terminology server (offline_terminology.rb).
# Inferno loads each file in lib/ as it boots, this one after the kit's.
COPY offline_terminology.rb lib/zz_mendari_offline_terminology.rb
# The list of a run's tests, with their short ids and requirement ids (list_tests.rb).
COPY list_tests.rb /opt/mendari/list_tests.rb

RUN useradd --create-home --uid 10001 inferno \
 && mkdir -p data log tmp \
 && chown -R inferno data log tmp
USER inferno
# An empty session database (SQLite, in data/): each container starts from it, so no run sees
# another's sessions or results.
RUN bundle exec inferno migrate

EXPOSE 4567
CMD ["bundle", "exec", "puma"]
