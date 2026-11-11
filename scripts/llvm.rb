require "global"
require "cmd/install"
require "formula_installer"
require "find"
require "json"
require_relative "llvm_ninja"

module LLVMStage
  ROOT = Pathname("/private/tmp/intelbrew-llvm")
  PAUSED = "LLVM stage reached its compilation deadline"

  module Installer
    def build
      super
    rescue RuntimeError => e
      normalized = e.message.lines.map(&:strip)
      raise unless formula.name == "llvm" && normalized.include?("RuntimeError: #{PAUSED}")
      raise unless (ROOT/"build"/".ninja_log").file?

      throw :llvm_paused
    end

    def build_args(formula_path)
      args = super
      return args unless formula.name == "llvm"

      index = args.index(HOMEBREW_LIBRARY_PATH/"build.rb")
      raise "Homebrew build entry point changed" unless index

      args[index] = __FILE__
      args
    end

    def add_build_sandbox_rules(sandbox, formula_path, **options)
      super
      return unless formula.name == "llvm"

      sandbox.allow_read_if_exists(path: Pathname(__FILE__))
      sandbox.allow_read_if_exists(path: Pathname(__dir__)/"llvm_ninja.rb")
      sandbox.allow_read(path: ROOT, type: :subpath)
      sandbox.allow_write_path(ROOT)
    end
  end

  module Recipe
    def stage(**options, &block)
      raise "Unexpected LLVM fetch stage" if options[:staging_path]

      source = ROOT/"source"
      source.mkdir
      super(**options.merge(staging_path: source), &block)
    end

    def system(command, *args)
      if command == "cmake" && args.include?("-B") && args.include?("Ninja")
        prepare_llvm_directory(args)
      elsif command == "cmake" && args.first == "--build" && !args.include?("install")
        raise "Unsupported LLVM build targets" unless args.length == 2

        directory = (Pathname.pwd/args.last).realpath
        raise "Unrecognized LLVM build directory" unless directory == @llvm_directory

        deadline = Integer((ROOT/"deadline").read)
        ohai "Compiling LLVM until #{Time.at(deadline).utc}"
        return if LLVMNinja.compile(directory, logs/"stage-ninja.log", deadline)

        directory.rename(ROOT/"build")
        raise PAUSED
      end
      super
    end

    private

    def prepare_llvm_directory(args)
      directory = (Pathname.pwd/args.fetch(args.index("-B") + 1)).expand_path
      relative = directory.relative_path_from(buildpath)
      if relative.to_s == "." || relative.each_filename.include?("..")
        raise "Unsafe LLVM build directory"
      end
      raise "Unexpected second LLVM configuration" if @llvm_directory

      @llvm_directory = directory
      command = [Pathname.pwd.to_s, *args.map(&:to_s)]
      epoch = Time.at(Integer(ENV.fetch("SOURCE_DATE_EPOCH")))
      brew_home = (buildpath/".brew_home").to_s
      Find.find(buildpath.to_s) do |path|
        Find.prune if path == brew_home
        stat = File.lstat(path)
        File.utime(epoch, epoch, path) if stat.file? && stat.mtime > epoch
      end

      previous = ROOT/"build"
      if previous.directory?
        raise "LLVM configuration changed" unless JSON.parse((ROOT/"configure.json").read) == command
        raise "LLVM build directory already exists" if directory.exist?

        previous.rename(directory)
      else
        (ROOT/"configure.json").write(JSON.generate(command))
      end
    end
  end
end

$stdout.sync = true
$stderr.sync = true
if ARGV.first == "install"
  ARGV.shift
  FormulaInstaller.prepend(LLVMStage::Installer)
  catch(:llvm_paused) do
    Homebrew::Cmd::InstallCmd.new(["--build-bottle", "--formula", "homebrew/core/llvm"]).run
    exit(Homebrew.failed? ? 1 : 0)
  end
  exit 75
else
  Formula.prepend(LLVMStage::Recipe)
  $PROGRAM_NAME = (HOMEBREW_LIBRARY_PATH/"build.rb").to_s
  load $PROGRAM_NAME
end
