import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../core/api/api_client.dart';
import '../../core/auth/auth_state.dart';
import '../../core/config/server_config.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';

class LoginScreen extends StatefulWidget {
  final bool registerMode;
  const LoginScreen({super.key, required this.registerMode});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _form = GlobalKey<FormState>();
  final _email = TextEditingController();
  final _pw = TextEditingController();
  final _pw2 = TextEditingController();
  final _server = TextEditingController();
  bool _busy = false;
  bool _checking = false;
  bool _obscure = true;
  bool _advanced = false;
  String? _error;

  /// Result of the last explicit/implicit health check (shown under the field).
  HealthResult? _health;

  @override
  void initState() {
    super.initState();
    _email.text = context.read<AuthState>().email ?? '';
    _server.text = context.read<ServerConfig>().base;
  }

  @override
  void dispose() {
    _email.dispose();
    _pw.dispose();
    _pw2.dispose();
    _server.dispose();
    super.dispose();
  }

  /// GET {base}/health; on success persists the (normalized) URL.
  Future<bool> _checkServer() async {
    final server = context.read<ServerConfig>();
    setState(() {
      _checking = true;
      _health = null;
    });
    final r = await server.check(_server.text);
    if (r.ok && r.base != null) await server.set(r.base!);
    if (!mounted) return r.ok;
    setState(() {
      _checking = false;
      _health = r;
      if (r.base != null) _server.text = r.base!;
      if (!r.ok) _advanced = true; // show the field that needs fixing
    });
    return r.ok;
  }

  Future<void> _submit() async {
    final form = _form.currentState;
    if (form == null || !form.validate()) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final auth = context.read<AuthState>();
    try {
      if (!await _checkServer()) {
        if (mounted) setState(() => _error = S.serverProblem);
        return;
      }
      if (widget.registerMode) {
        await auth.register(_email.text.trim(), _pw.text);
      } else {
        await auth.login(_email.text.trim(), _pw.text);
      }
      // Router redirect handles navigation.
    } on ApiException catch (e) {
      if (mounted) {
        setState(() => _error = switch (e.statusCode) {
              401 || 403 => S.wrongCredentials,
              409 => S.emailTaken,
              422 => S.invalidData,
              _ => errorText(e),
            });
      }
    } catch (e) {
      if (mounted) setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final reg = widget.registerMode;
    final h = _health;
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Form(
                key: _form,
                child: AutofillGroup(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    const _Logo(),
                    const SizedBox(height: 12),
                    Text(S.welcome, textAlign: TextAlign.center, style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 32),
                    TextFormField(
                      controller: _email,
                      keyboardType: TextInputType.emailAddress,
                      autocorrect: false,
                      autofillHints: const [AutofillHints.email],
                      textInputAction: TextInputAction.next,
                      decoration: const InputDecoration(labelText: S.email, prefixIcon: Icon(Icons.mail_outline)),
                      validator: (v) => (v == null || !RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(v.trim()))
                          ? S.invalidEmail
                          : null,
                    ),
                    const SizedBox(height: 12),
                    TextFormField(
                      controller: _pw,
                      obscureText: _obscure,
                      autocorrect: false,
                      enableSuggestions: false,
                      autofillHints: [reg ? AutofillHints.newPassword : AutofillHints.password],
                      decoration: InputDecoration(
                        labelText: S.password,
                        prefixIcon: const Icon(Icons.lock_outline),
                        suffixIcon: IconButton(
                          tooltip: _obscure ? S.showPassword : S.hidePassword,
                          icon: Icon(_obscure ? Icons.visibility : Icons.visibility_off),
                          onPressed: () => setState(() => _obscure = !_obscure),
                        ),
                      ),
                      validator: (v) => (v == null || v.length < 8) ? S.passwordTooShort : null,
                      onFieldSubmitted: reg ? null : (_) => _submit(),
                    ),
                    if (reg) ...[
                      const SizedBox(height: 12),
                      TextFormField(
                        controller: _pw2,
                        obscureText: _obscure,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration:
                            const InputDecoration(labelText: S.passwordConfirm, prefixIcon: Icon(Icons.lock_outline)),
                        validator: (v) => v != _pw.text ? S.passwordMismatch : null,
                        onFieldSubmitted: (_) => _submit(),
                      ),
                    ],
                    if (_error != null) ...[
                      const SizedBox(height: 16),
                      Text(_error!, style: const TextStyle(color: Brand.danger), textAlign: TextAlign.center),
                    ],
                    const SizedBox(height: 24),
                    FilledButton(
                      onPressed: _busy ? null : _submit,
                      child: _busy
                          ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2))
                          : Text(reg ? S.register : S.login),
                    ),
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: _busy ? null : () => context.go(reg ? '/login' : '/register'),
                      child: Text(reg ? S.haveAccount : S.noAccount),
                    ),
                    const SizedBox(height: 16),
                    // ---- advanced: server URL (MVP: cloud on the dev laptop) ----
                    TextButton.icon(
                      onPressed: () => setState(() => _advanced = !_advanced),
                      icon: Icon(_advanced ? Icons.expand_less_rounded : Icons.expand_more_rounded),
                      label: const Text(S.advancedSettings),
                    ),
                    if (_advanced) ...[
                      const SizedBox(height: 8),
                      TextField(
                        controller: _server,
                        keyboardType: TextInputType.url,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration: InputDecoration(
                          labelText: S.serverLabel,
                          hintText: context.read<ServerConfig>().defaultBase,
                          helperText: S.serverHelp,
                          helperMaxLines: 3,
                          prefixIcon: const Icon(Icons.dns_outlined),
                        ),
                        onChanged: (_) {
                          if (_health != null) setState(() => _health = null);
                        },
                      ),
                      const SizedBox(height: 8),
                      Row(children: [
                        OutlinedButton.icon(
                          onPressed: _checking || _busy ? null : () => _checkServer(),
                          icon: _checking
                              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                              : const Icon(Icons.network_check_rounded),
                          label: const Text(S.serverCheck),
                        ),
                        const SizedBox(width: 8),
                        TextButton(
                          onPressed: _checking || _busy
                              ? null
                              : () => setState(() {
                                    _server.text = context.read<ServerConfig>().defaultBase;
                                    _health = null;
                                  }),
                          child: const Text(S.serverReset),
                        ),
                      ]),
                      if (h != null) ...[
                        const SizedBox(height: 8),
                        Text(
                          S.healthMessage(h.status, h.detail),
                          style: TextStyle(color: h.ok ? Brand.ok : Brand.danger),
                        ),
                      ],
                    ],
                  ]),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Logo extends StatelessWidget {
  const _Logo();
  @override
  Widget build(BuildContext context) => Column(children: [
        Container(
          width: 96,
          height: 96,
          decoration: const BoxDecoration(color: Brand.blue, shape: BoxShape.circle),
          child: const Icon(Icons.pets_rounded, size: 52, color: Brand.yellow),
        ),
        const SizedBox(height: 12),
        Text(S.appName,
            style: Theme.of(context).textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w800, color: Brand.blue)),
      ]);
}
