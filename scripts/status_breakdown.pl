#!/usr/bin/env perl
# Status breakdown of a common-format access log: request count and
# transferred bytes per HTTP status code, with a bar chart.
#
# Demonstrates the Perl script style: the log path is $ARGV[0], CLI flags
# arrive after it, artifacts can be written to the current directory.
use strict;
use warnings;
use utf8;
binmode(STDOUT, ":utf8");

my $top      = 10;
my $by_bytes = 0;

my $log = shift @ARGV;
while (defined(my $arg = shift @ARGV)) {
    if    ($arg eq "--top")      { $top      = shift @ARGV // $top; }
    elsif ($arg eq "--by-bytes") { $by_bytes = 1; }
}

unless (defined $log && -f $log) {
    print STDERR "usage: perl status_breakdown.pl <access.log> [--top N] [--by-bytes]\n";
    exit 2;
}

my $LINE = qr/^\S+ \S+ \S+ \[[^\]]+\] "\S+ \S+ [^"]*" (\d{3}) (\d+|-)\s*$/;

my (%req, %bytes);
my ($total_req, $total_bytes) = (0, 0);

open(my $fh, "<", $log) or do { print STDERR "cannot read $log: $!\n"; exit 2; };
while (my $line = <$fh>) {
    my ($status, $b) = $line =~ $LINE or next;
    my $n = $b eq "-" ? 0 : $b;
    $req{$status}++;
    $bytes{$status} += $n;
    $total_req++;
    $total_bytes += $n;
}
close($fh);

if ($total_req == 0) {
    print "no access-log lines found (expected common log format)\n";
    exit 0;
}

sub human {
    my ($b) = @_;
    return sprintf("%.1f MiB", $b / 1048576) if $b >= 1048576;
    return sprintf("%.1f KiB", $b / 1024)    if $b >= 1024;
    return "$b B";
}

my @codes =
      $by_bytes ? (sort { $bytes{$b} <=> $bytes{$a} } keys %bytes)
                : (sort { $req{$b} <=> $req{$a} || $a <=> $b } keys %req);
@codes = @codes[0 .. ($#codes > $top - 1 ? $top - 1 : $#codes)];

my $max = $by_bytes
    ? (sort { $b <=> $a } map { $bytes{$_} } @codes)[0]
    : (sort { $b <=> $a } map { $req{$_} } @codes)[0];
my $width = 30;

printf "Status breakdown — %d requests, %s transferred, %d status codes\n\n",
    $total_req, human($total_bytes), scalar(keys %req);
printf "%-5s %8s %12s  %s\n", "code", "reqs", "bytes", "";
for my $code (@codes) {
    my $val = $by_bytes ? $bytes{$code} : $req{$code};
    my $bar = "█" x ($val * $width / $max);
    printf "%-5s %8d %12s  %s\n", $code, $req{$code}, human($bytes{$code}), $bar;
}
