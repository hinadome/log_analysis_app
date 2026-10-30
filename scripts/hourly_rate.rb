#!/usr/bin/env ruby
# Event rate histogram: buckets timestamped log lines per minute or per hour
# and draws a bar chart, so rate spikes are easy to spot.
#
# Demonstrates the Ruby script style: the log path is ARGV[0], CLI flags
# arrive after it, artifacts can be written to the current directory.
interval = "minute"
level = nil

log = ARGV.shift
until ARGV.empty?
  arg = ARGV.shift
  case arg
  when "--interval" then interval = ARGV.shift
  when "--level" then level = ARGV.shift
  end
end

abort "usage: ruby hourly_rate.rb <log> [--interval minute|hour] [--level ERROR]" unless log
abort "interval must be 'minute' or 'hour'" unless %w[minute hour].include?(interval)

counts = Hash.new(0)
unless File.file?(log)
  warn "cannot read #{log}"
  exit 2
end

File.foreach(log) do |line|
  next if level && !line.include?(level)
  # accepts "2026-10-28 14:00:00.123 LEVEL ..." and "2026-10-28T14:00:00 ..." styles
  m = line.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})/)
  next unless m
  bucket = interval == "minute" ? "#{m[1]} #{m[2]}:#{m[3]}" : "#{m[1]} #{m[2]}:00"
  counts[bucket] += 1
end

if counts.empty?
  puts "no timestamped lines found#{level ? " (filtered by #{level})" : ""}"
  exit 0
end

width = 40
max = counts.values.max
label = level ? "events containing #{level}" : "events"
puts "Events per #{interval} — #{counts.values.sum} #{label} in #{counts.size} buckets"
puts
counts.keys.sort.each do |bucket|
  bar = "█" * (counts[bucket] * width / max)
  puts "#{bucket}  #{bar} #{counts[bucket]}"
end
