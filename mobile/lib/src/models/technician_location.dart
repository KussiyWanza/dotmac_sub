/// Live technician position for an in-progress work order + the rating result,
/// proxied from the CRM via the sub (/me + /reseller work-order endpoints).
class TechnicianLocation {
  const TechnicianLocation({
    required this.available,
    this.reason,
    this.latitude,
    this.longitude,
    this.accuracyM,
    this.updatedAt,
    this.estimatedArrivalAt,
  });

  final bool available;

  /// Why the map is hidden when [available] is false (e.g. not_in_progress,
  /// sharing_off, no_fix, stale, not_linked).
  final String? reason;
  final double? latitude;
  final double? longitude;
  final double? accuracyM;
  final DateTime? updatedAt;
  final DateTime? estimatedArrivalAt;

  factory TechnicianLocation.fromJson(Map<String, dynamic> json) =>
      TechnicianLocation(
        available: json['available'] == true,
        reason: json['reason']?.toString(),
        latitude: (json['latitude'] as num?)?.toDouble(),
        longitude: (json['longitude'] as num?)?.toDouble(),
        accuracyM: (json['accuracy_m'] as num?)?.toDouble(),
        updatedAt: json['updated_at'] != null
            ? DateTime.tryParse(json['updated_at'].toString())
            : null,
        estimatedArrivalAt: json['estimated_arrival_at'] != null
            ? DateTime.tryParse(json['estimated_arrival_at'].toString())
            : null,
      );
}

class TechnicianRatingResult {
  const TechnicianRatingResult({
    required this.ok,
    required this.alreadyRated,
    this.rating,
  });

  final bool ok;
  final bool alreadyRated;
  final int? rating;

  factory TechnicianRatingResult.fromJson(Map<String, dynamic> json) =>
      TechnicianRatingResult(
        ok: json['ok'] == true,
        alreadyRated: json['already_rated'] == true,
        rating: (json['rating'] as num?)?.toInt(),
      );
}
