# The corresponding-source image a release publishes beside each image, as `<tag>-sources`: the
# files scripts/image_sources.py collected (its build context), and nothing else. It holds no
# program; licenses/image/SOURCE.md says how to copy the files out.
FROM scratch
COPY . /sources/
