#!/usr/bin/env python

import sys
sys.path.append("debian/lib/python")

import os
import os.path
import subprocess
import argparse

from debian_linux import config
from debian_linux.debian import *
from debian_linux.gencontrol import Gencontrol as Base
from debian_linux.utils import Templates, read_control
from pprint import pprint


class Gencontrol(Base):
    config_schema = {
        'abi': {
            'ignore-changes': config.SchemaItemList(),
        },
        'build': {
            'debug-info': config.SchemaItemBoolean(),
            'modules': config.SchemaItemBoolean(),
        },
        'description': {
            'parts': config.SchemaItemList(),
        },
        'image': {
            'bootloaders': config.SchemaItemList(),
            'configs': config.SchemaItemList(),
            'initramfs': config.SchemaItemBoolean(),
            'initramfs-generators': config.SchemaItemList(),
        },
        'relations': {
        },
        'xen': {
            'flavours': config.SchemaItemList(),
            'versions': config.SchemaItemList(),
        }
    }

    def __init__(self, config_dirs=["debian/config"], template_dirs=["debian/templates"]):
        super(Gencontrol, self).__init__(config.ConfigCoreHierarchy(self.config_schema, config_dirs), Templates(template_dirs), VersionLinux)
        self.process_changelog()
        self.config_dirs = config_dirs

    def _setup_makeflags(self, names, makeflags, data):
        for src, dst, optional in names:
            if src in data or not optional:
                makeflags[dst] = data[src]

    def do_main_setup(self, vars, makeflags, extra):
        super(Gencontrol, self).do_main_setup(vars, makeflags, extra)
        makeflags.update({
            'VERSION': self.version.linux_version,
            'UPSTREAMVERSION': self.version.linux_upstream,
            'ABINAME': self.abiname,
            'ABINAME_PART': self.abiname_part,
            'SOURCEVERSION': self.version.complete,
        })

    def do_main_makefile(self, makefile, makeflags, extra):
        fs_enabled = [featureset
                      for featureset in self.config['base', ]['featuresets']
                      if self.config.merge('base', None, featureset).get('enabled', True)]
        for featureset in fs_enabled:
            makeflags_featureset = makeflags.copy()
            makeflags_featureset['FEATURESET'] = featureset
            cmds_source = ["$(MAKE) -f debian/rules.real source-featureset %s"
                           % makeflags_featureset]
            makefile.add('source_%s_real' % featureset, cmds=cmds_source)
            makefile.add('source_%s' % featureset,
                         ['source_%s_real' % featureset])
            makefile.add('source', ['source_%s' % featureset])

        makeflags = makeflags.copy()
        makeflags['ALL_FEATURESETS'] = ' '.join(fs_enabled)
        super(Gencontrol, self).do_main_makefile(makefile, makeflags, extra)

    def do_main_packages(self, packages, vars, makeflags, extra):
        packages.extend(self.process_packages(self.templates["control.main"], self.vars))

    arch_makeflags = (
        ('kernel-arch', 'KERNEL_ARCH', False),
    )

    def do_arch_setup(self, vars, makeflags, arch, extra):
        config_base = self.config.merge('base', arch)
        self._setup_makeflags(self.arch_makeflags, makeflags, config_base)

    def do_arch_packages(self, packages, makefile, arch, vars, makeflags, extra):
        # Some userland architectures require kernels from another
        # (Debian) architecture, e.g. x32/amd64.
        foreign_kernel = not self.config['base', arch].get('featuresets')

        if self.version.linux_modifier is None:
            try:
                vars['abiname'] = '-%s' % self.config['abi', arch]['abiname']
            except KeyError:
                vars['abiname'] = self.abiname
            makeflags['ABINAME'] = vars['abiname']

        if foreign_kernel:
            packages_headers_arch = []
            makeflags['FOREIGN_KERNEL'] = True
        else:
            headers_arch = self.templates["control.headers.arch"]
            packages_headers_arch = self.process_packages(headers_arch, vars)

        libc_dev = self.templates["control.libc-dev"]
        packages_headers_arch[0:0] = self.process_packages(libc_dev, {})

        packages_headers_arch[-1]['Depends'].extend(PackageRelation())
        extra['headers_arch_depends'] = packages_headers_arch[-1]['Depends']

        self.merge_packages(packages, packages_headers_arch, arch)

        cmds_binary_arch = ["$(MAKE) -f debian/rules.real binary-arch-arch %s" % makeflags]
        makefile.add('binary-arch_%s_real' % arch, cmds=cmds_binary_arch)

        # Shortcut to aid architecture bootstrapping
        makefile.add('binary-libc-dev_%s' % arch,
                     ['source_none_real'],
                     ["$(MAKE) -f debian/rules.real install-libc-dev_%s %s" %
                      (arch, makeflags)])

        if os.getenv('DEBIAN_KERNEL_DISABLE_INSTALLER'):
            if self.changelog[0].distribution == 'UNRELEASED':
                import warnings
                warnings.warn(u'Disable installer modules on request (DEBIAN_KERNEL_DISABLE_INSTALLER set)')
            else:
                raise RuntimeError(u'Unable to disable installer modules in release build (DEBIAN_KERNEL_DISABLE_INSTALLER set)')
        else:
            # Add udebs using kernel-wedge
            installer_def_dir = 'debian/installer'
            installer_arch_dir = os.path.join(installer_def_dir, arch)
            if os.path.isdir(installer_arch_dir):
                kw_env = os.environ.copy()
                kw_env['KW_DEFCONFIG_DIR'] = installer_def_dir
                kw_env['KW_CONFIG_DIR'] = installer_arch_dir
                kw_proc = subprocess.Popen(
                    ['kernel-wedge', 'gen-control',
                     self.abiname],
                    stdout=subprocess.PIPE,
                    env=kw_env)
                udeb_packages = read_control(kw_proc.stdout)
                kw_proc.wait()
                if kw_proc.returncode != 0:
                    raise RuntimeError('kernel-wedge exited with code %d' %
                                       kw_proc.returncode)

                self.merge_packages(packages, udeb_packages, arch)

                # These packages must be built after the per-flavour/
                # per-featureset packages.  Also, this won't work
                # correctly with an empty package list.
                if udeb_packages:
                    makefile.add(
                        'binary-arch_%s' % arch,
                        cmds=["$(MAKE) -f debian/rules.real install-udeb_%s %s "
                              "PACKAGE_NAMES='%s'" %
                              (arch, makeflags,
                               ' '.join(p['Package'] for p in udeb_packages))])

    def do_featureset_setup(self, vars, makeflags, arch, featureset, extra):
        config_base = self.config.merge('base', arch, featureset)
        makeflags['LOCALVERSION_HEADERS'] = vars['localversion_headers'] = vars['localversion']

    def do_featureset_packages(self, packages, makefile, arch, featureset, vars, makeflags, extra):
        headers_featureset = self.templates["control.headers.featureset"]
        package_headers = self.process_package(headers_featureset[0], vars)

        self.merge_packages(packages, (package_headers,), arch)

        cmds_binary_arch = ["$(MAKE) -f debian/rules.real binary-arch-featureset %s" % makeflags]
        makefile.add('binary-arch_%s_%s_real' % (arch, featureset), cmds=cmds_binary_arch)
        makefile.include('debian/rules.featureset-%s' % featureset)

    flavour_makeflags_base = (
        ('compiler', 'COMPILER', False),
        ('kernel-arch', 'KERNEL_ARCH', False),
        ('cflags', 'CFLAGS_KERNEL', True),
        ('override-host-type', 'OVERRIDE_HOST_TYPE', True),
    )

    flavour_makeflags_image = (
        ('type', 'TYPE', False),
        ('initramfs', 'INITRAMFS', True),
    )

    flavour_makeflags_other = (
        ('localversion', 'LOCALVERSION', False),
        ('localversion-image', 'LOCALVERSION_IMAGE', True),
    )

    def do_flavour_setup(self, vars, makeflags, arch, featureset, flavour, extra):
        config_base = self.config.merge('base', arch, featureset, flavour)
        config_description = self.config.merge('description', arch, featureset, flavour)
        config_image = self.config.merge('image', arch, featureset, flavour)

        vars['class'] = config_description['hardware']
        vars['longclass'] = config_description.get('hardware-long') or vars['class']

        vars['localversion-image'] = vars['localversion']
        override_localversion = config_image.get('override-localversion', None)
        if override_localversion is not None:
            vars['localversion-image'] = vars['localversion_headers'] + '-' + override_localversion

        self._setup_makeflags(self.flavour_makeflags_base, makeflags, config_base)
        self._setup_makeflags(self.flavour_makeflags_image, makeflags, config_image)
        self._setup_makeflags(self.flavour_makeflags_other, makeflags, vars)

    def do_flavour_packages(self, packages, makefile, arch, featureset, flavour, vars, makeflags, extra):
        headers = self.templates["control.headers"]

        config_entry_base = self.config.merge('base', arch, featureset, flavour)
        config_entry_build = self.config.merge('build', arch, featureset, flavour)
        config_entry_description = self.config.merge('description', arch, featureset, flavour)
        config_entry_image = self.config.merge('image', arch, featureset, flavour)
        config_entry_relations = self.config.merge('relations', arch, featureset, flavour)

        compiler = config_entry_base.get('compiler', 'gcc')
        relations_compiler = PackageRelation(config_entry_relations[compiler])
        relations_compiler_build_dep = PackageRelation(config_entry_relations[compiler])
        for group in relations_compiler_build_dep:
            for item in group:
                item.arches = [arch]
        packages['source']['Build-Depends'].extend(relations_compiler_build_dep)

        image_fields = {'Description': PackageDescription()}
        for field in 'Depends', 'Provides', 'Suggests', 'Recommends', 'Conflicts', 'Breaks':
            field_val = config_entry_image.get(field.lower(), None)
            if isinstance(field_val,basestring):
                # allow e.g. 'Provides:' values to be templated in
                # 'definitions' files
                field_val %= dict(vars.items(),
                                  flavour=flavour,featureset=featureset)
            pkg_rel = PackageRelation(field_val,
                                      override_arches=(arch,))
            image_fields[field] = pkg_rel
        

        if config_entry_image.get('initramfs', True):
            generators = config_entry_image['initramfs-generators']
            l = PackageRelationGroup()
            for i in generators:
                i = config_entry_relations.get(i, i)
                l.append(i)
                a = PackageRelationEntry(i)
                if a.operator is not None:
                    a.operator = -a.operator
                    image_fields['Breaks'].append(PackageRelationGroup([a]))
            for item in l:
                item.arches = [arch]
            image_fields['Depends'].append(l)

        bootloaders = config_entry_image.get('bootloaders')
        if bootloaders:
            l = PackageRelationGroup()
            for i in bootloaders:
                i = config_entry_relations.get(i, i)
                l.append(i)
                a = PackageRelationEntry(i)
                if a.operator is not None:
                    a.operator = -a.operator
                    image_fields['Breaks'].append(PackageRelationGroup([a]))
            for item in l:
                item.arches = [arch]
            image_fields['Suggests'].append(l)

        desc_parts = self.config.get_merge('description', arch, featureset, flavour, 'parts')
        if desc_parts:
            # XXX: Workaround, we need to support multiple entries of the same name
            parts = list(set(desc_parts))
            parts.sort()
            desc = image_fields['Description']
            for part in parts:
                desc.append(config_entry_description['part-long-' + part])
                desc.append_short(config_entry_description.get('part-short-' + part, ''))

        packages_dummy = []
        packages_own = []

        image = self.templates["control.image.type-%s" % config_entry_image['type']]

        config_entry_xen = self.config.merge('xen', arch, featureset, flavour)
        if config_entry_xen:
            p = self.process_packages(self.templates['control.xen-linux-system'], vars)
            l = PackageRelationGroup()
            for xen_flavour in config_entry_xen['flavours']:
                l.append("xen-system-%s" % xen_flavour)
            p[0]['Depends'].append(l)
            packages_dummy.extend(p)

        vars.setdefault('desc', None)

        packages_own.append(self.process_real_image(image[0], image_fields, vars))
        packages_own.extend(self.process_packages(image[1:], vars))

        if config_entry_build.get('modules', True):
            makeflags['MODULES'] = True
            package_headers = self.process_package(headers[0], vars)
            package_headers['Depends'].extend(relations_compiler)
            if featureset and featureset != 'none':
                # Add 'Provides: linux-headers-<featureset>'
                package_headers.setdefault('Provides',
                                           PackageRelationGroup()).append(
                    'linux-headers-%s' % featureset)
            packages_own.append(package_headers)
            if 'none' in self.config['base',arch]:
                extra['headers_arch_depends'].append(
                    '%s (= ${binary:Version})' % packages_own[-1]['Package'])

        build_debug = config_entry_build.get('debug-info')

        if os.getenv('DEBIAN_KERNEL_DISABLE_DEBUG'):
            if self.changelog[0].distribution == 'UNRELEASED':
                import warnings
                warnings.warn(u'Disable debug infos on request (DEBIAN_KERNEL_DISABLE_DEBUG set)')
                build_debug = False
            else:
                raise RuntimeError(u'Unable to disable debug infos in release build (DEBIAN_KERNEL_DISABLE_DEBUG set)')

        if build_debug:
            makeflags['DEBUG'] = True
            packages_own.extend(self.process_packages(self.templates['control.image-dbg'], vars))

        self.merge_packages(packages, packages_own + packages_dummy, arch)

        def get_config(*entry_name):
            entry_real = ('image',) + entry_name
            entry = self.config.get(entry_real, None)
            if entry is None:
                return None
            return entry.get('configs', None)

        def check_config_default(fail, f):
            for d in self.config_dirs[::-1]:
                f1 = d + '/' + f
                if os.path.exists(f1):
                    return [f1]
            if fail:
                raise RuntimeError("%s unavailable" % f)
            return []

        def check_config_files(files):
            ret = []
            for f in files:
                for d in self.config_dirs[::-1]:
                    f1 = d + '/' + f
                    if os.path.exists(f1):
                        ret.append(f1)
                        break
                else:
                    raise RuntimeError("%s unavailable" % f)
            return ret

        def check_config(default, fail, *entry_name):
            configs = get_config(*entry_name)
            if configs is None:
                return check_config_default(fail, default)
            return check_config_files(configs)

        kconfig = check_config('config', True)
        kconfig.extend(check_config("kernelarch-%s/config" % config_entry_base['kernel-arch'], False))
        kconfig.extend(check_config("%s/config" % arch, True, arch))
        kconfig.extend(check_config("%s/config.%s" % (arch, flavour), False, arch, None, flavour))
        kconfig.extend(check_config("featureset-%s/config" % featureset, False, None, featureset))
        kconfig.extend(check_config("%s/%s/config" % (arch, featureset), False, arch, featureset))
        kconfig.extend(check_config("%s/%s/config.%s" % (arch, featureset, flavour), False, arch, featureset, flavour))
        makeflags['KCONFIG'] = ' '.join(kconfig)
        if build_debug:
            makeflags['KCONFIG_OPTIONS'] = '-o DEBUG_INFO=y'

        cmds_binary_arch = ["$(MAKE) -f debian/rules.real binary-arch-flavour %s" % makeflags]
        if packages_dummy:
            cmds_binary_arch.append("$(MAKE) -f debian/rules.real install-dummy DH_OPTIONS='%s' %s" % (' '.join(["-p%s" % i['Package'] for i in packages_dummy]), makeflags))
        cmds_build = ["$(MAKE) -f debian/rules.real build-arch %s" % makeflags]
        cmds_setup = ["$(MAKE) -f debian/rules.real setup-flavour %s" % makeflags]
        makefile.add('binary-arch_%s_%s_%s_real' % (arch, featureset, flavour), cmds=cmds_binary_arch)
        makefile.add('build-arch_%s_%s_%s_real' % (arch, featureset, flavour), cmds=cmds_build)
        makefile.add('setup_%s_%s_%s_real' % (arch, featureset, flavour), cmds=cmds_setup)

    def merge_packages(self, packages, new, arch):
        for new_package in new:
            name = new_package['Package']
            if name in packages:
                package = packages.get(name)
                package['Architecture'].add(arch)

                for field in 'Depends', 'Provides', 'Suggests', 'Recommends', 'Conflicts':
                    if field in new_package:
                        if field in package:
                            v = package[field]
                            v.extend(new_package[field])
                        else:
                            package[field] = new_package[field]

            else:
                new_package['Architecture'] = arch
                packages.append(new_package)

    def process_changelog(self):
        act_upstream = self.changelog[0].version.upstream
        versions = []
        for i in self.changelog:
            if i.version.upstream != act_upstream:
                break
            versions.append(i.version)
        self.versions = versions
        version = self.version = self.changelog[0].version
        if self.version.linux_modifier is not None:
            self.abiname_part = ''
        else:
            self.abiname_part = '-%s' % self.config['abi', ]['abiname']
        self.abiname = self.version.linux_upstream + self.abiname_part
        self.vars = {
            'upstreamversion': self.version.linux_upstream,
            'version': self.version.linux_version,
            'source_upstream': self.version.upstream,
            'source_package': self.changelog[0].source,
            'abiname': self.abiname,
        }
        self.config['version', ] = {'source': self.version.complete, 'abiname': self.abiname}

        distribution = self.changelog[0].distribution
        if distribution in ('unstable', ):
            if (version.linux_revision_experimental or
                    version.linux_revision_other):
                raise RuntimeError("Can't upload to %s with a version of %s" %
                        (distribution, version))
        if distribution in ('experimental', ):
            if not version.linux_revision_experimental:
                raise RuntimeError("Can't upload to %s with a version of %s" %
                        (distribution, version))

    def process_real_image(self, entry, fields, vars):
        entry = self.process_package(entry, vars)
        for key, value in fields.iteritems():
            if key in entry:
                real = entry[key]
                real.extend(value)
            elif value:
                entry[key] = value
        return entry

    def write(self, packages, makefile):
        self.write_config()
        super(Gencontrol, self).write(packages, makefile)

    def write_config(self):
        f = file("debian/config.defines.dump", 'w')
        self.config.dump(f)
        f.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Debian package configurator')
    parser.add_argument('--list-featuresets', action='store_true')
    parser.add_argument('--dump-config', action='store_true')
    args = parser.parse_args()

    g = Gencontrol()

    if args.list_featuresets:
        print (' '.join(g.config['base',]['featuresets']))
        sys.exit(0)
    elif args.dump_config:
        pprint(g.__dict__['config'])
        sys.exit(0)

    g()
