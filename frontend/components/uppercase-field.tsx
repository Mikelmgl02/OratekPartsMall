'use client';

import { ComponentProps } from 'react';

function uppercase(element: HTMLInputElement | HTMLTextAreaElement) {
  const value = element.value;
  const start = element.selectionStart;
  const end = element.selectionEnd;
  element.value = value.toUpperCase();
  if (start !== null && end !== null) {
    element.setSelectionRange(value.slice(0, start).toUpperCase().length, value.slice(0, end).toUpperCase().length);
  }
}

const entryOptions = { autoComplete: 'off', autoCorrect: 'off', autoCapitalize: 'characters', spellCheck: false } as const;
const upperValue = (value: ComponentProps<'input'>['value']) => typeof value === 'string' ? value.toUpperCase() : value;

export function UppercaseInput(props: ComponentProps<'input'>) {
  return <input {...props} {...entryOptions} className={`uppercase-entry ${props.className || ''}`} value={upperValue(props.value)} defaultValue={upperValue(props.defaultValue)} onChange={event => {
    if (!(event.nativeEvent as InputEvent).isComposing) uppercase(event.currentTarget);
    props.onChange?.(event);
  }}/>;
}

export function UppercaseTextarea(props: ComponentProps<'textarea'>) {
  return <textarea {...props} {...entryOptions} className={`uppercase-entry ${props.className || ''}`} value={upperValue(props.value)} defaultValue={upperValue(props.defaultValue)} onChange={event => {
    if (!(event.nativeEvent as InputEvent).isComposing) uppercase(event.currentTarget);
    props.onChange?.(event);
  }}/>;
}
