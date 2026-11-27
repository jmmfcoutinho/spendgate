# ADR-0029: Audio duration is counted from the audio, not read from the container

**Status:** Accepted
**Date:** 2026-11-27

## Context

A transcription call is held at the price of its audio duration, and spendgate measures that
duration itself (`core/audio.py`, protocol.md section 2.5). For FLAC, MP4/M4A, Ogg, WAV and WebM
the measure came from fields the file declares about itself: FLAC `STREAMINFO.total_samples`, the
MP4 `mvhd`/`mdhd` durations, the granule position of the last Ogg page, WAV `fmt.byte_rate`, the
Matroska `Duration` and the block timecodes. A decoder ignores all of them: it decodes every packet
or frame in the file, and the provider bills what it decoded. Only MP3 was counted (frame by
frame).

So a caller could send a file whose header understated its length by orders of magnitude and be
held for a fraction of what the provider then billed. The ledger booked the real charge afterwards
as overrun, but the call was admitted on a budget that could not pay for it, and every call in
flight could do the same: guarantee 2 in [how-money-is-protected.md](../how-money-is-protected.md)
(no call is admitted that could break the budget) did not hold for transcription. "Where a
container offers two measures the larger wins" protected against a missing value, not against two
values the writer chose.

## Decision

The measure counts what a decoder will decode, or a bound that is never below it, in every
container, and a declared length can only raise that count (M-16):

- **Ogg Opus:** every audio packet's duration from its TOC byte (RFC 6716 section 3.1); an empty
  or invalid packet counts as 120 ms, the longest a packet can be. **Ogg Vorbis:** every audio
  packet at half the long block size from the identification header, the most one packet can
  decode to. Granule positions that go backwards are refused; the largest granule only raises the
  count. The pre-skip and the end trim are not subtracted.
- **FLAC:** the frame headers are found and checked as a decoder checks them (sync code, no
  reserved value, a well-formed frame number, CRC-8), and their block sizes are summed at each
  frame's own rate. As for MP3, the file is refused when the frames cannot hold its bytes (each
  frame at most its uncompressed size) beyond 2% or 8 KiB.
- **MP4 / M4A:** each sound track's sample count (`stsz`, plus every fragment's `trun`) times 1024
  samples at the core rate of its AudioSpecificConfig. Only AAC is accepted (object types whose
  frame is at most 1024 core samples; USAC and other codecs are refused, as are compact `stz2`
  tables and duplicate track ids). Media data larger than the sample tables describe, beyond 2% or
  8 KiB, is refused.
- **WAV:** the data bytes divided by `sample_rate x block_align`. Only PCM, IEEE float, A-law and
  mu-law are accepted; a `fmt` chunk whose `block_align` or `byte_rate` disagrees with the rate,
  channels and sample width is refused, and so is a second `fmt` chunk. Bytes after the data chunk
  that are not a well-formed chunk are counted as samples.
- **WebM / Matroska:** every block of an audio track counted like Ogg (Opus by TOC, a laced Opus
  packet at 120 ms; Vorbis at half the long block from `CodecPrivate`); the timecodes and the
  declared `Duration` only raise the count. Only Opus and Vorbis audio tracks are accepted; a block
  for an undeclared track, duplicate track numbers and audio with a `ContentEncoding` are refused.
- **A size floor backs every count:** a file holding more audio bytes than its counted length can
  carry at the codec's highest bitrate (1275 bytes per Opus frame and stream, 768 bytes per AAC
  frame and channel, 32-bit PCM for Vorbis; the frame-coverage checks for FLAC and MP3) is refused,
  beyond 2% or 8 KiB. If a count were ever wrong, an understated length would be refused here.
- **The work stays bounded on hostile input:** every parse is one linear pass, with caps on what a
  file may contain (2 million Ogg lacing values, 300,000 FLAC sync candidates, 100,000 MP4 boxes per
  level, the existing 1 million Matroska elements), alongside the existing bounds (EBML integers of
  at most 8 bytes, a measured duration of at most 24 hours).

Every refusal is the existing one for unmeasurable audio: `invalid_request` with `param=file`,
before any hold or provider contact.

## Consequences

- A transcription hold is never below what the provider will decode, whatever the header says.
  Holds rise slightly for some files: Opus by the pre-skip and end trim (a few ms), partial final
  AAC and Opus frames rounded up to a whole frame. Vorbis is held at the long block for every
  packet: exact for long blocks, which speech and music mostly use, and up to the ratio of the
  block sizes (8 times for the usual 2048 and 256) for a stream of short blocks only.
- Files that were accepted are now refused: MP4 audio in a codec other than AAC (ALAC, MP3 in
  MP4, Opus in MP4, xHE-AAC) and MP4 without sample tables; WAV in ADPCM or another compressed
  format, or with inconsistent `fmt` fields; Matroska or WebM without a Tracks element, with an
  audio codec other than Opus or Vorbis, or with content encoding; Ogg whose granules go backwards;
  files whose bytes their frames or tables cannot account for; and inputs past the new caps, such
  as an Ogg file of more than 2 million packets (about 11 hours of 20 ms Opus).
- FLAC files that do not declare their total length (`total_samples` of 0, as written by some
  streaming encoders) are now accepted: their frames are counted.
- Measuring a 25 MB upload costs one pass over it; the slowest hostile inputs found take about as
  long as the existing Matroska element cap allows (around a second on a laptop).
- Assumed, not proven: that the provider decodes the file as a standard demuxer and decoder do
  (every packet of the stream, AAC at its AudioSpecificConfig rate, FLAC frames that pass their
  header CRC), and that it bills one audio track. Several audio tracks or streams are added up, so
  the hold is not below either assumption.
