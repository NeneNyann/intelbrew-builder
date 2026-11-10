require "digest"
require "download_strategy"

module RetainedSources
  module_function

  def git_source(resource)
    revision = resource.specs[:revision]
    unless revision.is_a?(String) && revision.match?(/\A[0-9a-f]{40}\z/) &&
           !resource.specs.key?(:branch) && !resource.specs.key?(:revisions)
      abort "Git source requires a full commit revision: #{resource.url}"
    end

    {
      "kind" => "git",
      "revision" => revision,
      "tag" => resource.specs[:tag],
      "path" => resource.cached_download.to_s,
      "cache_root" => resource.downloader.cache.to_s,
    }
  end

  def archive_source(resource)
    unless resource.checksum && resource.download_strategy <= CurlDownloadStrategy
      abort "Source requires a checksummed download or a pinned Git commit: #{resource.url}"
    end

    cached = resource.fetch
    unless cached.file? && !cached.symlink?
      abort "Downloaded source is not a regular file: #{resource.url}"
    end

    {
      "path" => cached.to_s,
      "sha256" => Digest::SHA256.file(cached).hexdigest,
      "size" => cached.size,
    }
  end

  def collect(formula)
    abort "Formula has no stable source: #{formula.name}" unless formula.stable

    resources = [formula.stable.resource, *formula.resources]
    formula.patchlist.each do |patch|
      resources << patch.resource if patch.respond_to?(:resource) && patch.resource
    end

    output = $stdout
    $stdout = $stderr
    begin
      resources.each_with_index.map do |resource, index|
        source =
          if resource.download_strategy <= GitDownloadStrategy
            git_source(resource)
          else
            archive_source(resource)
          end
        source.merge("index" => index, "url" => resource.url)
      end
    ensure
      $stdout = output
    end
  end
end
