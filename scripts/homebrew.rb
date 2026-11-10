require "json"
require "formula"
require "formulary"
require "tab"
require "utils/bottles"
require "utils/spdx"
require "stringio"

def core_formula(requested_name)
  unless requested_name&.match?(/\A[a-z0-9][a-z0-9+_.-]*(?:@[0-9][a-z0-9+_.-]*)?\z/)
    raise ArgumentError, "Invalid formula name"
  end

  formula = Formulary.factory("homebrew/core/#{requested_name}")

  unless formula.name == requested_name && formula.tap&.name == "homebrew/core"
    raise ArgumentError, "Expected a canonical homebrew/core formula: #{requested_name}"
  end

  formula
end

def available_bottle(formula)
  bottle = formula.bottle_for_tag(Utils::Bottles.tag)
  return unless bottle && formula.pour_bottle? && bottle.compatible_locations?

  {
    "url" => bottle.url,
    "sha256" => bottle.resource.checksum.hexdigest,
  }
end

def formula_metadata(formula)
  {
    "name" => formula.name,
    "version" => formula.version.to_s,
    "revision" => formula.revision,
    "version_scheme" => formula.version_scheme,
    "pkg_version" => formula.pkg_version.to_s,
    "license" => SPDX.license_expression_to_string(formula.license),
    "path" => formula.path.to_s,
    "bottle" => available_bottle(formula),
    "disabled" => formula.disabled?,
  }
end

def direct_dependencies(formula, build:)
  require "cmd/deps"
  arguments = ["--direct", "--formula"]
  arguments.concat(["--include-build", "--include-test", "--include-implicit"]) if build
  arguments << formula.full_name
  output = $stdout
  capture = StringIO.new
  begin
    $stdout = capture
    Homebrew::Cmd::Deps.new(arguments).run
  ensure
    $stdout = output
  end
  capture.string.lines.map { |line| line.strip.delete_prefix("homebrew/core/") }.uniq.sort
end

operation, requested_name = ARGV

if operation == "graph"
  Formulary.enable_factory_cache!
  request = JSON.parse(requested_name)
  pending = request.fetch("roots").dup
  forced = request.fetch("force")
  graph = {}
  until pending.empty?
    requested = pending.pop
    next if graph.key?(requested)
    begin
      if requested.start_with?("homebrew/cask/")
        require_relative "casks"
        record = CaskDependencies.metadata(requested)
        graph[requested] = record
        pending.concat(record["dependencies"])
        next
      end
      formula = core_formula(requested)
      record = formula_metadata(formula)
      bottle = record["bottle"]
      record["provider"] =
        if bottle && bottle["url"].start_with?("https://ghcr.io/v2/homebrew/core/")
          "official"
        elsif bottle && bottle["url"].start_with?(request.fetch("project_prefix")) &&
              !forced.include?(requested)
          "project"
        else
          "build"
        end
      runtime = direct_dependencies(formula, build: false)
      record["runtime_dependencies"] = runtime
      record["dependencies"] =
        record["provider"] == "build" ? direct_dependencies(formula, build: true) : runtime
      graph[requested] = record
      pending.concat(record["dependencies"])
    rescue StandardError => error
      graph[requested] = { "error" => error.message }
    end
  end
  puts JSON.generate(graph)
  exit
end

formula = core_formula(requested_name)

result =
  case operation
  when "sources"
    require_relative "sources"
    RetainedSources.collect(formula)
  when "receipt"
    unless formula.latest_version_installed?
      abort "Expected formula version is not installed: #{requested_name} #{formula.pkg_version}"
    end

    tab = Tab.for_formula(formula)

    {
      "name" => formula.name,
      "pkg_version" => formula.pkg_version.to_s,
      "tap" => tab.source["tap"],
      "poured_from_bottle" => tab.poured_from_bottle,
      "built_as_bottle" => tab.built_as_bottle,
    }
  else
    abort "Unknown metadata operation"
  end

puts JSON.generate(result)
