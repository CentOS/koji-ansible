#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils._text import to_native
from ansible.errors import AnsibleError
from collections import defaultdict
import common_koji

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()


ANSIBLE_METADATA = {
    'metadata_version': '1.0',
    'status': ['preview'],
    'supported_by': 'community'
}


DOCUMENTATION = '''
---
module: koji_tag

short_description: Create and manage Koji tags
description:
   - Create and manage Koji tags
options:
   name:
     description:
       - The name of the Koji tag to create and manage.
     required: true
   inheritance:
     description:
       - How to set inheritance. what happens when it's unset.
   external_repos:
     description:
       - list of Koji external repos to set for this tag. Each element of the
         list should have a "name" (external repo name) and "priority"
         (integer).
   packages:
     description:
       - dict of package owners and the a lists of packages each owner
         maintains.
   arches:
     description:
       - space-separated string of arches this Koji tag supports.
   perm:
     description:
       - permission (string or int) for this Koji tag.
   locked:
     description:
       - whether to lock this tag or not.
     choices: [true, false]
     default: false
   locked:
     description:
       - whether to lock this tag or not.
     choices: [true, false]
     default: false
   maven_support:
     description:
       - whether Maven repos should be generated for the tag.
     choices: [true, false]
     default: false
   maven_include_all:
     description:
       - include every build in this tag (including multiple versions of the
         same package) in the Maven repo.
     choices: [true, false]
     default: false
   extra:
     description:
       - set any extra parameters on this tag.
requirements:
  - "python >= 2.7"
  - "koji"
'''

EXAMPLES = '''
- name: create a main koji tag and candidate tag
  hosts: localhost
  tasks:
    - name: Create a main product koji tag
      koji_tag:
        koji: kojidev
        name: ceph-3.1-rhel-7
        arches: x86_64
        state: present
        packages:
          kdreyer:
            - ansible
            - ceph
            - ceph-ansible

    - name: Create a candidate koji tag
      koji_tag:
        koji: kojidev
        name: ceph-3.1-rhel-7-candidate
        state: present
        inheritance:
        - parent: ceph-3.1-rhel-7
          priority: 0

    - name: Create a tag that uses an external repo
      koji_tag:
        koji: kojidev
        name: storage7-ceph-nautilus-el7-build
        state: present
        external_repos:
        - repo: centos7-cr
          priority: 5
'''

RETURN = ''' # '''

perm_cache = {}


def get_perm_id(session, name):
    global perm_cache
    if not perm_cache:
        perm_cache = dict([
            (perm['name'], perm['id']) for perm in session.getAllPerms()
        ])
    return perm_cache[name]


def ensure_external_repos(session, tag_name, check_mode, repos):
    """
    Ensure that these external repos are configured on this Koji tag.

    :param session: Koji client session
    :param str tag_name: Koji tag name
    :param bool check_mode: don't make any changes
    :param list repos: ensure these external repos are set, and no others.
    """
    result = {'changed': False, 'stdout_lines': []}
    current_repo_list = session.getTagExternalRepos(tag_name)
    current = {repo['external_repo_name']: repo for repo in current_repo_list}
    for repo in repos:
        repo_name = repo['repo']
        repo_priority = repo['priority']
        if repo_name in current:
            # The repo is present for this tag.
            # Now ensure the priority is correct.
            if repo_priority == current[repo_name]['priority']:
                continue
            result['changed'] = True
            msg = 'set %s repo priority to %i' % (repo_name, repo_priority)
            result['stdout_lines'].append(msg)
            if not check_mode:
                common_koji.ensure_logged_in(session)
                session.editTagExternalRepo(tag_name, repo_name, repo_priority)
            continue
        result['changed'] = True
        msg = 'add %s external repo to %s' % (repo_name, tag_name)
        result['stdout_lines'].append(msg)
        if not check_mode:
            common_koji.ensure_logged_in(session)
            session.addExternalRepoToTag(tag_name, repo_name, repo_priority)
    # Find the repos to remove from this tag.
    repo_names = [repo['repo'] for repo in repos]
    current_names = current.keys()
    repos_to_remove = set(current_names) - set(repo_names)
    for repo_name in repos_to_remove:
        result['changed'] = True
        msg = 'removed %s repo from %s tag' % (repo_name, tag_name)
        result['stdout_lines'].append(msg)
        if not check_mode:
            common_koji.ensure_logged_in(session)
            session.removeExternalRepoFromTag(tag_name, repo_name)
    return result


def ensure_tag(session, name, check_mode, inheritance, external_repos,
               packages, **kwargs):
    """
    Ensure that this tag exists in Koji.

    :param session: Koji client session
    :param name: Koji tag name
    :param check_mode: don't make any changes
    :param inheritance: Koji tag inheritance settings. These will be translated
                        for Koji's setInheritanceData RPC.
    :param external_repos: Koji external repos to set for this tag.
    :param packages: dict of packages to add ("whitelist") for this tag.
                     If this is an empty dict, we don't touch the package list
                     for this tag.
    :param **kwargs: Pass remaining kwargs directly into Koji's createTag and
                     editTag2 RPCs.
    """
    taginfo = session.getTag(name)
    result = {'changed': False, 'stdout_lines': []}
    if not taginfo:
        if check_mode:
            result['stdout_lines'].append('would create tag %s' % name)
            result['changed'] = True
            return result
        common_koji.ensure_logged_in(session)
        if 'perm' in kwargs and kwargs['perm']:
            kwargs['perm'] = get_perm_id(session, kwargs['perm'])
        id_ = session.createTag(name, parent=None, **kwargs)
        result['stdout_lines'].append('created tag id %d' % id_)
        result['changed'] = True
        taginfo = {'id': id_}  # populate for inheritance management below
    else:
        # The tag name already exists. Ensure all the parameters are set.
        edits = {}
        for key, value in kwargs.items():
            if taginfo[key] != value:
                edits[key] = value
        # Find out which "extra" items we must explicitly remove
        # ("remove_extra" argument to editTag2).
        if 'extra' in kwargs:
            for key in taginfo['extra']:
                if key not in kwargs['extra']:
                    if 'remove_extra' not in edits:
                        edits['remove_extra'] = []
                    edits['remove_extra'].append(key)
        if edits:
            result['stdout_lines'].append(str(edits))
            result['changed'] = True
            if not check_mode:
                common_koji.ensure_logged_in(session)
                session.editTag2(name, **edits)
    # Ensure inheritance rules are all set.
    rules = []
    for rule in inheritance:
        parent_name = rule['parent']
        parent_taginfo = session.getTag(parent_name)
        if not parent_taginfo:
            raise ValueError("parent tag '%s' not found" % parent_name)
        parent_id = parent_taginfo['id']
        new_rule = {
            'child_id': taginfo['id'],
            'intransitive': False,
            'maxdepth': None,
            'name': parent_name,
            'noconfig': False,
            'parent_id': parent_id,
            'pkg_filter': '',
            'priority': rule['priority']}
        rules.append(new_rule)
    current_inheritance = session.getInheritanceData(name)
    if current_inheritance != rules:
        result['stdout_lines'].append('inheritance is %s' % inheritance)
        result['changed'] = True
        if not check_mode:
            common_koji.ensure_logged_in(session)
            session.setInheritanceData(name, rules, clear=True)
    # Ensure external repos.
    if external_repos is not None:
        repos_result = ensure_external_repos(session, name, check_mode,
                                             external_repos)
        if repos_result['changed']:
            result['changed'] = True
        result['stdout_lines'].extend(repos_result['stdout_lines'])
    # Ensure package list.
    if packages:
        # Note: this in particular could really benefit from koji's
        # multicalls...
        common_koji.ensure_logged_in(session)
        current_pkgs = session.listPackages(tagID=taginfo['id'])
        current_names = set([pkg['package_name'] for pkg in current_pkgs])
        # Create a "current_owned" dict to compare with what's in Ansible.
        current_owned = defaultdict(set)
        for pkg in current_pkgs:
            owner = pkg['owner_name']
            pkg_name = pkg['package_name']
            current_owned[owner].add(pkg_name)
        for owner, owned in packages.items():
            for package in owned:
                if package not in current_names:
                    # The package was missing from the tag entirely.
                    if not check_mode:
                        session.packageListAdd(name, package, owner)
                    result['stdout_lines'].append('added pkg %s' % package)
                    result['changed'] = True
                else:
                    # The package is already in this tag.
                    # Verify ownership.
                    if package not in current_owned.get(owner, []):
                        if not check_mode:
                            session.packageListSetOwner(name, package, owner)
                        result['stdout_lines'].append('set %s owner %s' %
                                                      (package, owner))
                        result['changed'] = True
        # Delete any packages not in Ansible.
        all_names = [name for names in packages.values() for name in names]
        delete_names = set(current_names) - set(all_names)
        for package in delete_names:
            result['stdout_lines'].append('remove pkg %s' % package)
            result['changed'] = True
            if not check_mode:
                session.packageListRemove(name, package, owner)
    return result


def delete_tag(session, name, check_mode):
    """ Ensure that this tag is deleted from Koji. """
    taginfo = session.getTag(name)
    result = dict(
        stdout='',
        changed=False,
    )
    if taginfo:
        result['stdout'] = 'deleted tag %d' % taginfo['id']
        result['changed'] = True
        if not check_mode:
            common_koji.ensure_logged_in(session)
            session.deleteTag(name)
    return result


def run_module():
    module_args = dict(
        koji=dict(type='str', required=False),
        name=dict(type='str', required=True),
        state=dict(type='str', required=False, default='present'),
        inheritance=dict(type='list', required=False, default=[]),
        external_repos=dict(type='list', required=False, default=None),
        packages=dict(type='dict', required=False, default={}),
        arches=dict(type='str', required=False, default=None),
        perm=dict(type='str', required=False, default=None),
        locked=dict(type='bool', required=False, default=False),
        maven_support=dict(type='bool', required=False, default=False),
        maven_include_all=dict(type='bool', required=False, default=False),
        extra=dict(type='dict', required=False, default={}),
    )
    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    if not common_koji.HAS_KOJI:
        module.fail_json(msg='koji is required for this module')

    check_mode = module.check_mode
    params = module.params
    profile = params['koji']
    name = params['name']
    state = params['state']

    session = common_koji.get_session(profile)

    if state == 'present':
        try:
            result = ensure_tag(session, name,
                                check_mode,
                                inheritance=params['inheritance'],
                                external_repos=params['external_repos'],
                                packages=params['packages'],
                                arches=params['arches'],
                                perm=params['perm'] or None,
                                locked=params['locked'],
                                maven_support=params['maven_support'],
                                maven_include_all=params['maven_include_all'],
                                extra=params['extra'])
        except Exception as e:
            raise AnsibleError(
                    "koji_tag ensure_tag '%s' failed:\n%s\nparameters:\n%s"
                    % (name, to_native(e), params))
    elif state == 'absent':
        try:
            result = delete_tag(session, name, check_mode)
        except Exception as e:
            raise AnsibleError(
                    "koji_tag delete_tag '%s' failed:\n%s"
                    % (name, to_native(e)))
    else:
        module.fail_json(msg="State must be 'present' or 'absent'.",
                         changed=False, rc=1)

    module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()
