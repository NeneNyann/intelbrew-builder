require "json"
require "fileutils"
require "find"
require "prism"
require "formula"
require "formula_installer"

module LLVMCheckpoint
  ROOT = Pathname("/private/tmp/intelbrew-llvm")
  JOURNAL = ROOT/"commands.json"

  class Pause < Exception
  end

  class Boundaries < Prism::Visitor
    attr_reader :lines

    def initialize
      @lines = []
    end

    def visit_call_node(node)
      if node.name == :system && node.arguments
        values = node.arguments.arguments.map do |argument|
          argument.unescaped if argument.is_a?(Prism::StringNode)
        end
        if values.take(2) == ["cmake", "--build"] && !values.include?("install")
          @lines << node.location.start_line
        end
      end
      super
    end
  end

  def self.boundaries(path)
    result = Prism.parse_file(path.to_s)
    raise "Cannot parse the LLVM recipe" unless result.success?

    visitor = Boundaries.new
    result.value.accept(visitor)
    raise "LLVM recipe has no recognized build boundaries" if visitor.lines.empty?

    visitor.lines
  end

  def self.read
    JOURNAL.exist? ? JSON.parse(JOURNAL.read) : { "commands" => [], "directories" => [] }
  end

  def self.write(state)
    temporary = ROOT/"commands.next.json"
    temporary.write(JSON.generate(state))
    File.rename(temporary, JOURNAL)
  end

  def self.pause(state)
    state["paused"] = true
    write(state)
    raise Pause, "LLVM checkpoint saved"
  end

  module Installer
    def build_args(formula_path)
      arguments = super
      return arguments unless formula.name == "llvm"

      arguments.map do |argument|
        argument.to_s == (HOMEBREW_LIBRARY_PATH/"build.rb").to_s ? __FILE__ : argument
      end
    end

    def add_build_sandbox_rules(sandbox, formula_path, **options)
      super
      sandbox.allow_read_if_exists(path: Pathname(__FILE__)) if formula.name == "llvm"
    end
  end

  module FormulaMethods
    def stage(**options, &block)
      return super unless name == "llvm"

      @llvm_state = LLVMCheckpoint.read
      @llvm_cursor = 0
      @llvm_installing = false
      @llvm_restored = false
      @llvm_boundaries = LLVMCheckpoint.boundaries(path)
      @llvm_started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      puts "LLVM recipe: #{@llvm_boundaries.length} build boundaries"
      singleton_class.prepend(ArchiveMethods)
      source = ROOT/"source"
      source.mkpath
      super(**options.merge(staging_path: source, staged: false), &block)
    end

    def llvm_restore
      return if @llvm_restored

      @llvm_restored = true
      previous = ROOT/"previous"
      return unless previous.directory?

      relative = buildpath.relative_path_from(ROOT/"source")
      original = previous/relative
      raise "LLVM source directory changed" unless original.directory?

      @llvm_state.fetch("directories").sort_by(&:length).each do |directory|
        relative = Pathname(directory)
        if relative.absolute? || relative.each_filename.include?("..") || directory == "."
          raise "Invalid LLVM build directory"
        end
        old = original/directory
        next unless old.exist?

        destination = buildpath/directory
        raise "LLVM build directory overlaps source files" if destination.exist?

        destination.parent.mkpath
        FileUtils.mv(old, destination)
      end

      Find.find(original.to_s) do |path|
        old = Pathname(path)
        Find.prune if old.directory? && old.basename.to_s == ".brew_home"
        next unless old.file? && !old.symlink?

        destination = buildpath/old.relative_path_from(original)
        next unless destination.file? && !destination.symlink?
        raise "LLVM staged source differs" unless FileUtils.compare_file(old, destination)

        stat = old.stat
        File.utime(stat.atime, stat.mtime, destination)
      end
      FileUtils.remove_entry(previous)
    end

    def system(command, *arguments)
      return super unless name == "llvm" && @llvm_state

      llvm_restore
      values = arguments.map(&:to_s)
      cmake = File.basename(command.to_s) == "cmake"
      install_target = values.include?("--target") && values.include?("install")
      @llvm_installing ||= cmake && (values.include?("--install") || install_target)
      return super if @llvm_installing

      identity = [Pathname.pwd.to_s, command.to_s, *values]
      previous = @llvm_state.fetch("commands")[@llvm_cursor]
      @llvm_cursor += 1
      if previous
        raise "LLVM command sequence changed" unless previous.fetch("arguments") == identity

        if previous.fetch("failed")
          raise BuildError.new(self, command, arguments, ENV.to_hash)
        end
        return nil
      end

      if cmake
        index = values.index("-B") || values.index("--build")
        destination = values[index + 1] if index
        destination ||= values.find { |value| value.start_with?("-B") }&.delete_prefix("-B")
        if destination
          directory = Pathname(File.expand_path(destination))
          relative = directory.relative_path_from(buildpath).to_s
          if relative == "." || relative.split("/").include?("..")
            raise "LLVM requires a build directory inside its source workspace"
          end
          @llvm_state["directories"] |= [relative]
        end
      end

      LLVMCheckpoint.write(@llvm_state)
      log = directory/".ninja_log" if cmake && values.include?("--build")
      before = [log.size, log.mtime] if log&.file?
      started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      begin
        result = super
      rescue BuildError
        @llvm_state["commands"] << { "arguments" => identity, "failed" => true }
        LLVMCheckpoint.write(@llvm_state)
        raise
      end
      @llvm_state["commands"] << { "arguments" => identity, "failed" => false }
      LLVMCheckpoint.write(@llvm_state)
      finished = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      location = caller_locations(1, 1).first
      boundary = location.path == path.to_s && @llvm_boundaries.include?(location.lineno)
      work_done = log&.file? && before != [log.size, log.mtime]
      if boundary && work_done && finished - started >= 60 && finished - @llvm_started >= 150 * 60
        LLVMCheckpoint.pause(@llvm_state)
      end
      result
    end
  end

  module ArchiveMethods
    def convert_lto_archive(archive)
      directory = ROOT/"converted"
      directory.mkpath
      cached = directory/archive.basename
      if cached.file?
        FileUtils.cp(cached, archive)
        return
      end

      super
      File.link(archive, cached)
    end
  end
end

if ARGV.first == "install"
  ARGV.shift
  require "cmd/install"
  FormulaInstaller.prepend(LLVMCheckpoint::Installer)
  begin
    Homebrew::Cmd::InstallCmd.new(
      ["--build-bottle", "--keep-tmp", "--formula", "homebrew/core/llvm"],
    ).run
  rescue Exception
    raise unless LLVMCheckpoint.read["paused"] == true

    puts "LLVM checkpoint saved"
    exit 0
  end
else
  Formula.prepend(LLVMCheckpoint::FormulaMethods)
  load HOMEBREW_LIBRARY_PATH/"build.rb"
end
