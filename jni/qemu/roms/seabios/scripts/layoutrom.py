#!/usr/bin/env python
# Script to analyze code and arrange ld sections.
#
# Copyright (C) 2008-2014  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import operator
import sys

# LD script headers/trailers
COMMONHEADER = """
/* DO NOT EDIT!  This is an autogenerated file.  See scripts/layoutrom.py. */
OUTPUT_FORMAT("elf32-i386")
OUTPUT_ARCH("i386")
SECTIONS
{
"""
COMMONTRAILER = """

        /* Discard regular data sections to force a link error if
         * code attempts to access data not marked with VAR16 (or other
         * appropriate macro)
         */
        /DISCARD/ : {
                *(.text*) *(.data*) *(.bss*) *(.rodata*)
                *(COMMON) *(.discard*) *(.eh_frame) *(.note*)
                }
}
"""


######################################################################
# Determine section locations
######################################################################

# Align 'pos' to 'alignbytes' offset
def alignpos(pos, alignbytes):
    mask = alignbytes - 1
    return (pos + mask) & ~mask

# Determine the final addresses for a list of sections that end at an
# address.
def setSectionsStart(sections, endaddr, minalign=1, segoffset=0):
    totspace = 0
    for section in sections:
        if section.align > minalign:
            minalign = section.align
        totspace = alignpos(totspace, section.align) + section.size
    startaddr = int((endaddr - totspace) / minalign) * minalign
    curaddr = startaddr
    for section in sections:
        curaddr = alignpos(curaddr, section.align)
        section.finalloc = curaddr
        section.finalsegloc = curaddr - segoffset
        curaddr += section.size
    return startaddr, minalign

# The 16bit code can't exceed 64K of space.
BUILD_BIOS_ADDR = 0xf0000
BUILD_BIOS_SIZE = 0x10000
BUILD_ROM_START = 0xc0000
BUILD_LOWRAM_END = 0xa0000
# Space to reserve in f-segment for dynamic allocations
BUILD_MIN_BIOSTABLE = 2048

# Layout the 16bit code.  This ensures sections with fixed offset
# requirements are placed in the correct location.  It also places the
# 16bit code as high as possible in the f-segment.
def fitSections(sections, fillsections):
    # fixedsections = [(addr, section), ...]
    fixedsections = []
    for section in sections:
        if section.name.startswith('.fixedaddr.'):
            addr = int(section.name[11:], 16)
            section.finalloc = addr + BUILD_BIOS_ADDR
            section.finalsegloc = addr
            fixedsections.append((addr, section))
            if section.align != 1:
                print("Error: Fixed section %s has non-zero alignment (%d)" % (
                    section.name, section.align))
                sys.exit(1)
    fixedsections.sort(key=operator.itemgetter(0))
    firstfixed = fixedsections[0][0]

    # Find freespace in fixed address area
    # fixedAddr = [(freespace, section), ...]
    fixedAddr = []
    for i in range(len(fixedsections)):
        fixedsectioninfo = fixedsections[i]
        addr, section = fixedsectioninfo
        if i == len(fixedsections) - 1:
            nextaddr = BUILD_BIOS_SIZE
        else:
            nextaddr = fixedsections[i+1][0]
        avail = nextaddr - addr - section.size
        fixedAddr.append((avail, section))
    fixedAddr.sort(key=operator.itemgetter(0))

    # Attempt to fit other sections into fixed area
    canrelocate = [(section.size, section.align, section.name, section)
                   for section in fillsections]
    canrelocate.sort()
    canrelocate = [section for size, align, name, section in canrelocate]
    totalused = 0
    for freespace, fixedsection in fixedAddr:
        addpos = fixedsection.finalsegloc + fixedsection.size
        totalused += fixedsection.size
        nextfixedaddr = addpos + freespace
#        print("Filling section %x uses %d, next=%x, available=%d" % (
#            fixedsection.finalloc, fixedsection.size, nextfixedaddr, freespace))
        while 1:
            canfit = None
            for fitsection in canrelocate:
                if addpos + fitsection.size > nextfixedaddr:
                    # Can't fit and nothing else will fit.
                    break
                fitnextaddr = alignpos(addpos, fitsection.align) + fitsection.size
#                print("Test %s - %x vs %x" % (
#                    fitsection.name, fitnextaddr, nextfixedaddr))
                if fitnextaddr > nextfixedaddr:
                    # This item can't fit.
                    continue
                canfit = (fitnextaddr, fitsection)
            if canfit is None:
                break
            # Found a section that can fit.
            fitnextaddr, fitsection = canfit
            canrelocate.remove(fitsection)
            fitsection.finalloc = addpos + BUILD_BIOS_ADDR
            fitsection.finalsegloc = addpos
            addpos = fitnextaddr
            totalused += fitsection.size
#            print("    Adding %s (size %d align %d) pos=%x avail=%d" % (
#                fitsection[2], fitsection[0], fitsection[1]
#                , fitnextaddr, nextfixedaddr - fitnextaddr))

    # Report stats
    total = BUILD_BIOS_SIZE-firstfixed
    slack = total - totalused
    print ("Fixed space: 0x%x-0x%x  total: %d  slack: %d"
           "  Percent slack: %.1f%%" % (
            firstfixed, BUILD_BIOS_SIZE, total, slack,
            (float(slack) / total) * 100.0))

    return firstfixed + BUILD_BIOS_ADDR

# Return the subset of sections with a given category
def getSectionsCategory(sections, category):
    return [section for section in sections if section.category == category]

# Return the subset of sections with a given fileid
def getSectionsFileid(sections, fileid):
    return [section for section in sections if section.fileid == fileid]

# Return the subset of sections with a given name prefix
def getSectionsPrefix(sections, prefix):
    return [section for section in sections
            if section.name.startswith(prefix)]

# The sections (and associated information) to be placed in output rom
class LayoutInfo:
    sections = None
    genreloc = None
    sec32init_start = sec32init_end = sec32init_align = None
    sec32low_start = sec32low_end = None
    zonelow_base = final_sec32low_start = None
    zonefseg_start = zonefseg_end = None
    final_readonly_start = None
    varlowsyms = entrysym = None

# Determine final memory addresses for sections
def doLayout(sections, config, genreloc):
    li = LayoutInfo()
    li.sections = sections
    li.genreloc = genreloc
    # Determine 16bit positions
    sections16 = getSectionsCategory(sections, '16')
    textsections = getSectionsPrefix(sections16, '.text.')
    rodatasections = getSectionsPrefix(sections16, '.rodata')
    datasections = getSectionsPrefix(sections16, '.data16.')
    fixedsections = getSectionsCategory(sections, 'fixed')

    firstfixed = fitSections(fixedsections, textsections)
    remsections = [s for s in textsections+rodatasections+datasections
                   if s.finalloc is None]
    sec16_start, sec16_align = setSectionsStart(
        remsections, firstfixed, segoffset=BUILD_BIOS_ADDR)

    # Determine 32seg positions
    sections32seg = getSectionsCategory(sections, '32seg')
    textsections = getSectionsPrefix(sections32seg, '.text.')
    rodatasections = getSectionsPrefix(sections32seg, '.rodata')
    datasections = getSectionsPrefix(sections32seg, '.data32seg.')

    sec32seg_start, sec32seg_align = setSectionsStart(
        textsections + rodatasections + datasections, sec16_start
        , segoffset=BUILD_BIOS_ADDR)

    # Determine 32bit "fseg memory" data positions
    sections32textfseg = getSectionsCategory(sections, '32textfseg')
    sec32textfseg_start, sec32textfseg_align = setSectionsStart(
        sections32textfseg, sec32seg_start, 16)

    sections32fseg = getSectionsCategory(sections, '32fseg')
    sec32fseg_start, sec32fseg_align = setSectionsStart(
        sections32fseg, sec32textfseg_start, 16
        , segoffset=BUILD_BIOS_ADDR)

    # Determine 32flat runtime positions
    sections32flat = getSectionsCategory(sections, '32flat')
    textsections = getSectionsPrefix(sections32flat, '.text.')
    rodatasections = getSectionsPrefix(sections32flat, '.rodata')
    datasections = getSectionsPrefix(sections32flat, '.data.')
    bsssections = getSectionsPrefix(sections32flat, '.bss.')

    sec32flat_start, sec32flat_align = setSectionsStart(
        textsections + rodatasections + datasections + bsssections
        , sec32fseg_start, 16)

    # Determine 32flat init positions
    sections32init = getSectionsCategory(sections, '32init')
    init32_textsections = getSectionsPrefix(sections32init, '.text.')
    init32_rodatasections = getSectionsPrefix(sections32init, '.rodata')
    init32_datasections = getSectionsPrefix(sections32init, '.data.')
    init32_bsssections = getSectionsPrefix(sections32init, '.bss.')

    sec32init_start, sec32init_align = setSectionsStart(
        init32_textsections + init32_rodatasections
        + init32_datasections + init32_bsssections
        , sec32flat_start, 16)

    # Determine location of ZoneFSeg memory.
    zonefseg_end = sec32flat_start
    if not genreloc:
        zonefseg_end = sec32init_start
    zonefseg_start = BUILD_BIOS_ADDR
    if zonefseg_start + BUILD_MIN_BIOSTABLE > zonefseg_end:
        # Not enough ZoneFSeg space - force a minimum space.
        zonefseg_end = sec32fseg_start
        zonefseg_start = zonefseg_end - BUILD_MIN_BIOSTABLE
        sec32flat_start, sec32flat_align = setSectionsStart(
            textsections + rodatasections + datasections + bsssections
            , zonefseg_start, 16)
        sec32init_start, sec32init_align = setSectionsStart(
            init32_textsections + init32_rodatasections
            + init32_datasections + init32_bsssections
            , sec32flat_start, 16)
    li.sec32init_start = sec32init_start
    li.sec32init_end = sec32flat_start
    li.sec32init_align = sec32init_align
    final_readonly_start = min(BUILD_BIOS_ADDR, sec32flat_start)
    if not genreloc:
        final_readonly_start = min(BUILD_BIOS_ADDR, sec32init_start)
    li.zonefseg_start = zonefseg_start
    li.zonefseg_end = zonefseg_end
    li.final_readonly_start = final_readonly_start

    # Determine "low memory" data positions
    sections32low = getSectionsCategory(sections, '32low')
    sec32low_end = sec32init_start
    if config.get('CONFIG_MALLOC_UPPERMEMORY'):
        final_sec32low_end = final_readonly_start
        zonelow_base = final_sec32low_end - 64*1024
        zonelow_base = max(BUILD_ROM_START, alignpos(zonelow_base, 2*1024))
    else:
        final_sec32low_end = BUILD_LOWRAM_END
        zonelow_base = final_sec32low_end - 64*1024
    relocdelta = final_sec32low_end - sec32low_end
    li.sec32low_start, li.sec32low_align = setSectionsStart(
        sections32low, sec32low_end, 16
        , segoffset=zonelow_base - relocdelta)
    li.sec32low_end = sec32low_end
    li.zonelow_base = zonelow_base
    li.final_sec32low_start = li.sec32low_start + relocdelta

    # Print statistics
    size16 = BUILD_BIOS_ADDR + BUILD_BIOS_SIZE - sec16_start
    size32seg = sec16_start - sec32seg_start
    size32textfseg = sec32seg_start - sec32textfseg_start
    size32fseg = sec32textfseg_start - sec32fseg_start
    size32flat = sec32fseg_start - sec32flat_start
    size32init = sec32flat_start - sec32init_start
    sizelow = li.sec32low_end - li.sec32low_start
    print("16bit size:           %d" % size16)
    print("32bit segmented size: %d" % size32seg)
    print("32bit flat size:      %d" % (size32flat + size32textfseg))
    print("32bit flat init size: %d" % size32init)
    print("Lowmem size:          %d" % sizelow)
    print("f-segment var size:   %d" % size32fseg)
    return li


######################################################################
# Linker script output
######################################################################

# Write LD script includes for the given cross references
def outXRefs(sections, useseg=0, exportsyms=[], forcedelta=0):
    xrefs = dict([(symbol.name, symbol) for symbol in exportsyms])
    out = ""
    for section in sections:
        for reloc in section.relocs:
            symbol = reloc.symbol
            if (symbol.section is not None
                and (symbol.section.fileid != section.fileid
                     or symbol.name != reloc.symbolname)):
                xrefs[reloc.symbolname] = symbol
    for symbolname, symbol in xrefs.items():
        loc = symbol.section.finalloc
        if useseg:
            loc = symbol.section.finalsegloc
        out += "%s = 0x%x ;\n" % (symbolname, loc + forcedelta + symbol.offset)
    return out

# Write LD script includes for the given sections
def outSections(sections, useseg=0):
    out = ""
    for section in sections:
        loc = section.finalloc
        if useseg:
            loc = section.finalsegloc
        out += "%s 0x%x : { *(%s) }\n" % (section.name, loc, section.name)
    return out

# Write LD script includes for the given sections using relative offsets
def outRelSections(sections, startsym, useseg=0):
    sections = [(section.finalloc, section) for section in sections
                if section.finalloc is not None]
    sections.sort(key=operator.itemgetter(0))
    out = ""
    for addr, section in sections:
        loc = section.finalloc
        if useseg:
            loc = section.finalsegloc
        out += ". = ( 0x%x - %s ) ;\n" % (loc, startsym)
        if section.name in ('.rodata.str1.1', '.rodata'):
            out += "_rodata%s = . ;\n" % (section.fileid,)
        out += "*%s.*(%s)\n" % (section.fileid, section.name)
    return out

# Build linker script output for a list of relocations.
def strRelocs(outname, outrel, relocs):
    relocs.sort()
    return ("        %s_start = ABSOLUTE(.) ;\n" % (outname,)
            + "".join(["LONG(0x%x - %s)\n" % (pos, outrel)
                       for pos in relocs])
            + "        %s_end = ABSOLUTE(.) ;\n" % (outname,))

# Find relocations to the given sections
def getRelocs(sections, tosection, type=None):
    return [section.finalloc + reloc.offset
            for section in sections
                for reloc in section.relocs
                    if (reloc.symbol.section in tosection
                        and (type is None or reloc.type == type))]

# Output the linker scripts for all required sections.
def writeLinkerScripts(li, out16, out32seg, out32flat):
    # Write 16bit linker script
    filesections16 = getSectionsFileid(li.sections, '16')
    out = outXRefs(filesections16, useseg=1) + """
    zonelow_base = 0x%x ;
    _zonelow_seg = 0x%x ;

%s
""" % (li.zonelow_base,
       int(li.zonelow_base / 16),
       outSections(filesections16, useseg=1))
    outfile = open(out16, 'w')
    outfile.write(COMMONHEADER + out + COMMONTRAILER)
    outfile.close()

    # Write 32seg linker script
    filesections32seg = getSectionsFileid(li.sections, '32seg')
    out = (outXRefs(filesections32seg, useseg=1)
           + outSections(filesections32seg, useseg=1))
    outfile = open(out32seg, 'w')
    outfile.write(COMMONHEADER + out + COMMONTRAILER)
    outfile.close()

    # Write 32flat linker script
    sec32all_start = li.sec32low_start
    relocstr = ""
    if li.genreloc:
        # Generate relocations
        initsections = dict([
            (s, 1) for s in getSectionsCategory(li.sections, '32init')])
        noninitsections = dict([(s, 1) for s in li.sections
                                if s not in initsections])
        absrelocs = getRelocs(initsections, initsections, type='R_386_32')
        relrelocs = getRelocs(initsections, noninitsections, type='R_386_PC32')
        initrelocs = getRelocs(noninitsections, initsections)
        relocstr = (strRelocs("_reloc_abs", "code32init_start", absrelocs)
                    + strRelocs("_reloc_rel", "code32init_start", relrelocs)
                    + strRelocs("_reloc_init", "code32flat_start", initrelocs))
        numrelocs = len(absrelocs + relrelocs + initrelocs)
        sec32all_start -= numrelocs * 4
    filesections32flat = getSectionsFileid(li.sections, '32flat')
    out = outXRefs([], exportsyms=li.varlowsyms
                   , forcedelta=li.final_sec32low_start-li.sec32low_start)
    out += outXRefs(filesections32flat, exportsyms=[li.entrysym]) + """
    _reloc_min_align = 0x%x ;
    zonefseg_start = 0x%x ;
    zonefseg_end = 0x%x ;
    zonelow_base = 0x%x ;
    final_varlow_start = 0x%x ;
    final_readonly_start = 0x%x ;
    varlow_start = 0x%x ;
    varlow_end = 0x%x ;
    code32init_start = 0x%x ;
    code32init_end = 0x%x ;

    code32flat_start = 0x%x ;
    .text code32flat_start : {
%s
%s
        code32flat_end = ABSOLUTE(.) ;
    } :text
""" % (li.sec32init_align,
       li.zonefseg_start,
       li.zonefseg_end,
       li.zonelow_base,
       li.final_sec32low_start,
       li.final_readonly_start,
       li.sec32low_start,
       li.sec32low_end,
       li.sec32init_start,
       li.sec32init_end,
       sec32all_start,
       relocstr,
       outRelSections(li.sections, 'code32flat_start'))
    out = COMMONHEADER + out + COMMONTRAILER + """
ENTRY(%s)
PHDRS
{
        text PT_LOAD AT ( code32flat_start ) ;
}
""" % (li.entrysym.name,)
    outfile = open(out32flat, 'w')
    outfile.write(out)
    outfile.close()


######################################################################
# Detection of unused sections and init sections
######################################################################

# Visit all sections reachable from a given set of start sections
def findReachable(anchorsections, checkreloc, data):
    anchorsections = dict([(section, []) for section in anchorsections])
    pending = list(anchorsections)
    while pending:
        section = pending.pop()
        for reloc in section.relocs:
            chain = anchorsections[section] + [section.name]
            if not checkreloc(reloc, section, data, chain):
                continue
            nextsection = reloc.symbol.section
            if nextsection not in anchorsections:
                anchorsections[nextsection] = chain
                pending.append(nextsection)
    return anchorsections

# Find "runtime" sections (ie, not init only sections).
def checkRuntime(reloc, rsection, data, chain):
    section = reloc.symbol.section
    if section is None or '.init.' in section.name:
        return 0
    if '.data.varinit.' in section.name:
        print("ERROR: %s is VARVERIFY32INIT but used from %s" % (
            section.name, chain))
        sys.exit(1)
    return 1

# Find and keep the section associated with a symbol (if available).
def checkKeepSym(reloc, syms, fileid, isxref):
    symbolname = reloc.symbolname
    mustbecfunc = symbolname.startswith('_cfunc')
    if mustbecfunc:
        symprefix = '_cfunc' + fileid + '_'
        if not symbolname.startswith(symprefix):
            return 0
        symbolname = symbolname[len(symprefix):]
    symbol = syms.get(symbolname)
    if (symbol is None or symbol.section is None
        or symbol.section.name.startswith('.discard.')):
        return 0
    isdestcfunc = (symbol.section.name.startswith('.text.')
                   and not symbol.section.name.startswith('.text.asm.'))
    if ((mustbecfunc and not isdestcfunc)
        or (not mustbecfunc and isdestcfunc and isxref)):
        return 0

    reloc.symbol = symbol
    return 1

# Resolve a relocation and check if it should be kept in the final binary.
def checkKeep(reloc, section, symbols, chain):
    ret = checkKeepSym(reloc, symbols[section.fileid], section.fileid, 0)
    if ret:
        return ret
    # Not in primary sections - it may be a cross 16/32 reference
    for fileid in ('16', '32seg', '32flat'):
        if fileid != section.fileid:
            ret = checkKeepSym(reloc, symbols[fileid], fileid, 1)
            if ret:
                return ret
    return 0


######################################################################
# Startup and input parsing
######################################################################

class Section:
    name = size = alignment = fileid = relocs = None
    finalloc = finalsegloc = category = None
class Reloc:
    offset = type = symbolname = symbol = None
class Symbol:
    name = offset = section = None

# Read in output from objdump
def parseObjDump(file, fileid):
    # sections = [section, ...]
    sections = []
    sectionmap = {}
    # symbols[symbolname] = symbol
    symbols = {}

    state = None
    for line in file.readlines():
        line = line.rstrip()
        if line == 'Sections:':
            state = 'section'
            continue
        if line == 'SYMBOL TABLE:':
            state = 'symbol'
            continue
        if line.startswith('RELOCATION RECORDS FOR ['):
            sectionname = line[24:-2]
            if sectionname.startswith('.debug_'):
                # Skip debugging sections (to reduce parsing time)
                state = None
                continue
            state = 'reloc'
            relocsection = sectionmap[sectionname]
            continue

        if state == 'section':
            try:
                idx, name, size, vma, lma, fileoff, align = line.split()
                if align[:3] != '2**':
                    continue
                section = Section()
                section.name = name
                section.size = int(size, 16)
                section.align = 2**int(align[3:])
                section.fileid = fileid
                section.relocs = []
                sections.append(section)
                sectionmap[name] = section
            except ValueError:
                pass
            continue
        if state == 'symbol':
            try:
                parts = line[17:].split()
                if len(parts) == 3:
                    sectionname, size, name = parts
                elif len(parts) == 4 and parts[2] == '.hidden':
                    sectionname, size, hidden, name = parts
                else:
                    continue
                symbol = Symbol()
                symbol.size = int(size, 16)
                symbol.offset = int(line[:8], 16)
                symbol.name = name
                symbol.section = sectionmap.get(sectionname)
                symbols[name] = symbol
            except ValueError:
                pass
            continue
        if state == 'reloc':
            try:
                off, type, symbolname = line.split()
                reloc = Reloc()
                reloc.offset = int(off, 16)
                reloc.type = type
                reloc.symbolname = symbolname
                reloc.symbol = symbols.get(symbolname)
                if reloc.symbol is None:
                    # Some binutils (2.20.1) give section name instead
                    # of a symbol - create a dummy symbol.
                    reloc.symbol = symbol = Symbol()
                    symbol.size = 0
                    symbol.offset = 0
                    symbol.name = symbolname
                    symbol.section = sectionmap.get(symbolname)
                    symbols[symbolname] = symbol
                relocsection.relocs.append(reloc)
            except ValueError:
                pass
    return sections, symbols

# Parser for constants in simple C header files.
def scanconfig(file):
    f = open(file, 'r')
    opts = {}
    for l in f.readlines():
        parts = l.split()
        if len(parts) != 3:
            continue
        if parts[0] != '#define':
            continue
        value = parts[2]
        if value.isdigit() or (value.startswith('0x') and value[2:].isdigit()):
            value = int(value, 0)
        opts[parts[1]] = value
    return opts

def main():
    # Get output name
    in16, in32seg, in32flat, cfgfile, out16, out32seg, out32flat = sys.argv[1:]

    # Read in the objdump information
    infile16 = open(in16, 'r')
    infile32seg = open(in32seg, 'r')
    infile32flat = open(in32flat, 'r')

    # infoX = (sections, symbols)
    info16 = parseObjDump(infile16, '16')
    info32seg = parseObjDump(infile32seg, '32seg')
    info32flat = parseObjDump(infile32flat, '32flat')

    # Read kconfig config file
    config = scanconfig(cfgfile)

    # Figure out which sections to keep.
    allsections = info16[0] + info32seg[0] + info32flat[0]
    symbols = {'16': info16[1], '32seg': info32seg[1], '32flat': info32flat[1]}
    if config.get('CONFIG_COREBOOT'):
        entrysym = symbols['16'].get('entry_elf')
    elif config.get('CONFIG_CSM'):
        entrysym = symbols['16'].get('entry_csm')
    else:
        entrysym = symbols['16'].get('reset_vector')
    anchorsections = [entrysym.section] + [
        section for section in allsections
        if section.name.startswith('.fixedaddr.')]
    keepsections = findReachable(anchorsections, checkKeep, symbols)
    sections = [section for section in allsections if section in keepsections]

    # Separate 32bit flat into runtime, init, and special variable parts
    anchorsections = [
        section for section in sections
        if ('.data.varlow.' in section.name or '.data.varfseg.' in section.name
            or '.fixedaddr.' in section.name or '.runtime.' in section.name)]
    runtimesections = findReachable(anchorsections, checkRuntime, None)
    for section in sections:
        if section.name.startswith('.data.varlow.'):
            section.category = '32low'
        elif section.name.startswith('.data.varfseg.'):
            section.category = '32fseg'
        elif section.name.startswith('.text.32fseg.'):
            section.category = '32textfseg'
        elif section.name.startswith('.fixedaddr.'):
            section.category = 'fixed'
        elif section.fileid == '32flat' and section not in runtimesections:
            section.category = '32init'
        else:
            section.category = section.fileid

    # Determine the final memory locations of each kept section.
    genreloc = '_reloc_abs_start' in symbols['32flat']
    li = doLayout(sections, config, genreloc)

    # Exported symbols
    li.varlowsyms = [symbol for symbol in symbols['32flat'].values()
                     if (symbol.section is not None
                         and symbol.section.finalloc is not None
                         and '.data.varlow.' in symbol.section.name
                         and symbol.name != symbol.section.name)]
    li.entrysym = entrysym

    # Write out linker script files.
    writeLinkerScripts(li, out16, out32seg, out32flat)

if __name__ == '__main__':
    main()