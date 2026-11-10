require "cask/cask"
require "cask/cask_loader"

module CaskDependencies
  module_function

  def reference(value)
    token = value.delete_prefix("homebrew/cask/")
    unless token.match?(/\A[a-z0-9][a-z0-9+_.-]*(?:@[a-z0-9][a-z0-9+_.-]*)?\z/) &&
           !token.include?("..")
      raise ArgumentError, "Expected an official Cask dependency: #{value}"
    end
    "homebrew/cask/#{token}"
  end

  def metadata(requested)
    cask = Cask::CaskLoader.load(reference(requested))
    unless cask.tap&.name == "homebrew/cask" && reference(cask.token) == requested
      raise ArgumentError, "Expected a canonical homebrew/cask target: #{requested}"
    end
    raise "Cask is disabled: #{requested}" if cask.disabled?
    unless cask.platform_supported?(Utils::Bottles.tag)
      raise "Cask does not support macOS 15 Intel: #{requested}"
    end

    formulae = cask.depends_on.formula.map do |value|
      formula = value.delete_prefix("homebrew/core/")
      unless formula.match?(/\A[a-z0-9][a-z0-9+_.-]*(?:@[0-9][a-z0-9+_.-]*)?\z/) &&
             !formula.include?("..")
        raise ArgumentError, "Expected a Core Formula dependency: #{value}"
      end
      formula
    end
    casks = cask.depends_on.cask.map { |value| reference(value) }
    {
      "kind" => "cask",
      "name" => requested,
      "dependencies" => (formulae + casks).uniq.sort,
    }
  end
end
