"""Complete exception frame chains without credentials, response bodies or locals."""
import traceback


def safe_master_traceback(error):
    blocks=[];seen=set()
    def visit(current):
        if current is None or id(current) in seen:return
        seen.add(id(current))
        prior=current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if prior is not None:
            visit(prior)
            blocks.append('The above exception preceded this error:')
        blocks.append('Traceback (most recent call last):')
        for frame in traceback.extract_tb(current.__traceback__):
            blocks.append(f'  File {frame.filename}, line {frame.lineno}, in {frame.name}')
        # Arbitrary exception strings and local values can contain broker secrets.
        blocks.append(type(current).__name__+': exception message omitted from logs for credential safety')
    visit(error)
    return '\n'.join(blocks)
