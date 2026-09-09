// 密码强度前端校验：与后端 security.validate_password_strength 保持一致
// 用于注册页 / 修改密码弹窗的即时提示，后端仍会二次校验（前端只是体验优化）

const WEAK_PASSWORDS = [
  '12345678', '123456789', '1234567890', 'password', 'password1', 'qwerty123',
  'qwertyui', '11111111', '00000000', 'abc12345', 'abcd1234', '123123123',
  'admin123', 'administrator', 'a12345678', 'passw0rd', 'iloveyou', 'sunshine',
]

export interface PasswordCheck {
  ok: boolean
  reason: string
  /** 强度级别：weak=不合格 / medium=合格 / strong=很强（仅 ok=true 时有意义） */
  level: 'weak' | 'medium' | 'strong'
}

export function checkPassword(password: string, username = ''): PasswordCheck {
  if (!password || password.length < 8) return { ok: false, reason: '密码至少 8 位', level: 'weak' }
  if (password.length > 64) return { ok: false, reason: '密码最长 64 位', level: 'weak' }
  if (!/[A-Za-z]/.test(password) || !/\d/.test(password)) {
    return { ok: false, reason: '密码必须同时包含字母和数字', level: 'weak' }
  }
  if (WEAK_PASSWORDS.includes(password.toLowerCase())) {
    return { ok: false, reason: '这个密码太常见，容易被猜中，请换一个', level: 'weak' }
  }
  if (username.length >= 4 && password.toLowerCase().includes(username.toLowerCase())) {
    return { ok: false, reason: '密码不能包含用户名', level: 'weak' }
  }
  // 额外长度/复杂字符 → 更强
  const strong = password.length >= 12 && /[^A-Za-z0-9]/.test(password)
  return { ok: true, reason: strong ? '密码强度：很强' : '密码强度：合格', level: strong ? 'strong' : 'medium' }
}
