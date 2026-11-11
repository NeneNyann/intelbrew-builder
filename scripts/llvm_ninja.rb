module LLVMNinja
  def self.wait_for_ninja(pid, seconds)
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + seconds
    loop do
      result = Process.waitpid2(pid, Process::WNOHANG)
      return result.last if result

      remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
      return if remaining <= 0

      sleep [remaining, 5].min
    end
  end

  def self.compile(directory, log, deadline)
    remaining = deadline - Time.now.to_i
    raise "LLVM setup exhausted the compilation budget" unless remaining.positive?

    cache = (directory/"CMakeCache.txt").read
    ninja = cache[/^CMAKE_MAKE_PROGRAM:FILEPATH=(.+)$/, 1]
    unless ninja && File.basename(ninja) == "ninja" && File.executable?(ninja)
      raise "LLVM requires the configured Ninja executable"
    end

    pid = Process.spawn({ "HOMEBREW_CC_LOG_PATH" => log.to_s }, ninja, chdir: directory,
                        out: log.to_s, err: [:child, :out])
    status = wait_for_ninja(pid, remaining)
    interrupted = status.nil?
    if interrupted
      Process.kill("INT", pid)
      status = wait_for_ninja(pid, 90)
      unless status
        Process.kill("KILL", pid)
        wait_for_ninja(pid, 10)
        raise "Ninja did not exit after interruption"
      end
    end
    return true if status.success?

    Kernel.system("/usr/bin/tail", "-n", "20", log.to_s)
    clean_stop = interrupted && status.exitstatus == 130 &&
                 !File.foreach(log).any? { |line| line.start_with?("FAILED:") }
    raise "LLVM compilation failed (#{status})" unless clean_stop

    false
  end
end
