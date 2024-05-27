import os
import re
import subprocess as sp
import shutil

import_codes = """
import java.util.logging.Logger;
import java.lang.reflect.Field;
"""

"""
Logger logger = Logger.getLogger(this.getName());
List currentClassFields = Lists.newArrayList(this.getClass().getDeclaredFields());
for(Field field : this.getClass().getDeclaredFields()) {
    Field field = currentClassFields[i];
    field.setAccessible(true);
    try {
        String _name = field.getName();
        Object _value = field.get(this);
        logger.info(_name + " = " + _value);
    } catch (IllegalAccessException e) {
        e.printStackTrace();
    }
}
    """

symbol_print_codes = """
Logger logger = Logger.getLogger("logger");
Field[] currentClassFields = this.getClass().getDeclaredFields();
for (int i = 0; i < currentClassFields.length; i++) {
    Field field = currentClassFields[i];
    field.setAccessible(true);
    try {
        String _name = field.getName();
        Object _value = field.get(this);
        logger.info(_name + " = " + _value);
    } catch (IllegalAccessException e) {
        e.printStackTrace();
    }
}
    """


def get_test_info(project_name, bug_id):
    cmd = ['defects4j', 'info', '-p', project_name, '-b', str(bug_id)]
    out = sp.run(cmd, capture_output=True, text=True)

    cause_flag = False
    for line in out.stdout.split('\n'):
        if cause_flag and line.startswith(' - '):
            raw = line.strip()[2:]
            break
        if 'Root cause in triggering tests' in line:
            cause_flag = True

    file_name, function_name = raw.split('::')
    test_info = {
        'raw': raw,
        'file_name': file_name,
        'function_name': function_name,
    }
    return test_info


def checkout(project_name, bug_id):
    shutil.rmtree('./tmp')
    cmd = ['defects4j', 'checkout', '-p', project_name, '-v', f'{bug_id}b', '-w', './tmp']
    out = sp.run(cmd, capture_output=True, text=True)


def edit_java_file(project_name, bug_id, test_info):
    file_name = test_info['file_name']
    module_name = test_info['raw'].replace('::', '.')
    # parse error line
    failing_test_file_path = f'./defects4j/{project_name}_{bug_id}/failing_tests'
    with open(failing_test_file_path, 'r') as f:
        error_logs = f.readlines()
    
    error_line = None
    for error_log in error_logs[1:]:
        m = re.search(f'{test_info["function_name"]}\(\S+\:(\d+)\)', error_log)
        if m:
            error_line = int(m.group(1))
            break

    if error_line is None:
        return False

    file_name_map = {
        'Chart': os.path.join('./tmp/tests', f'{file_name.replace(".", "/")}.java'),
        'Closure': os.path.join('./tmp/test', f'{file_name.replace(".", "/")}.java'),
        # 'Lang': os.path.join('./tmp/src/test/java', f'{file_name.replace(".", "/")}.java'),
        'Lang': os.path.join('./tmp/src/test', f'{file_name.replace(".", "/")}.java'),
        # 'Math': os.path.join('./tmp/src/test/java', f'{file_name.replace(".", "/")}.java'),
        'Math': os.path.join('./tmp/src/test', f'{file_name.replace(".", "/")}.java'),
        'Time': os.path.join('./tmp/src/test/java', f'{file_name.replace(".", "/")}.java'),
        # 'Time': os.path.join('./tmp/src/test', f'{file_name.replace(".", "/")}.java'),
    }
    file_name = file_name_map[project_name]
    with open(file_name, 'r') as f:
        lines = f.readlines()

    new_lines = []
    import_flag = False
    function_flag = False
    variables = []

    for i, line in enumerate(lines):
        if i > error_line - 1:
            new_lines.append(line)
            continue

        if i == error_line - 1:
            m = re.match('(\s+)', line)
            indent = m.group(0)
            for code_line in symbol_print_codes.strip().split('\n'):
                new_lines.append(indent + code_line + '\n')

            for variable in variables:
                new_lines.append(indent + f'logger.info("{variable} = " + {variable});\n')

        if not import_flag and 'import' in line:
            new_lines.append(import_codes)
            import_flag = True

        if function_flag:
            m = re.search('\s(\w+)\s\=\s', line)
            if m:
                variables.append(m.group(1))

        if f'public void {test_info["function_name"]}()' in line:
            function_flag = True

        new_lines.append(line)

    with open(file_name, 'w') as f:
        f.write(''.join(new_lines))

    return True

def monitor_test(test_info):
    cmd = ['defects4j', 'monitor.test', '-t', test_info['raw']]
    out = sp.run(cmd, capture_output=True, text=True, cwd='./tmp/')
    symbol_tables = []

    monitor_flag = False
    for line in out.stderr.split('\n'):
        if monitor_flag:
            if 'INFO: ' in line:
                symbol_tables.append(line.split('INFO: ')[-1] + '\n')
        if line.startswith('monitor.test:'):
            monitor_flag = True
    symbol_tables = list(set(symbol_tables))
    return symbol_tables


def edit_failing_test_file(project_name, bug_id, symbol_tables):
    failing_test_file_path = f'./defects4j/{project_name}_{bug_id}/failing_tests'
    with open(failing_test_file_path, 'r') as f:
        lines = f.readlines()
    new_lines = symbol_tables + lines
    print(symbol_tables)
    with open(failing_test_file_path, 'w') as f:
        f.write(''.join(new_lines))


def run():
    project_names = ['Chart', 'Closure', 'Lang', 'Math', 'Time']
    max_bug_ids = [26, 133, 65, 106, 27]
    deprecated_bug_ids_list = [[], [63, 93], [2], [], [21]]
    project_names = ['Time']
    max_bug_ids = [27]
    deprecated_bug_ids_list = [[21]]
    for project_name, max_bug_id, deprecated_bug_ids in zip(project_names, max_bug_ids, deprecated_bug_ids_list):
        if project_name == 'Time':
            start = 21
        else:
            start = 1
        for bug_id in range(start, max_bug_id + 1):
            if bug_id in deprecated_bug_ids:
                continue
            print(project_name, bug_id)
            test_info = get_test_info(project_name, bug_id)
            checkout(project_name, bug_id)
            success = edit_java_file(project_name, bug_id, test_info)
            if not success:
                print('')
                continue
            symbol_tables = monitor_test(test_info)
            edit_failing_test_file(project_name, bug_id, symbol_tables)

    # project_name = 'Lang'
    # bug_id = 8
    # test_info = get_test_info(project_name, bug_id)
    # checkout(project_name, bug_id)
    # edit_java_file(project_name, bug_id, test_info)
    # symbol_tables = monitor_test(test_info)
    # edit_failing_test_file(project_name, bug_id, symbol_tables)


if __name__ == '__main__':
    run()