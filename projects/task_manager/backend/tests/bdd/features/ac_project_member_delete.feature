# ac.project_member.delete  (requirements: requirement.project_assignment; operations: operation.project_member.delete)
Feature: Delete Project Member succeeds

  Scenario: Delete Project Member succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Confirm deleting the Project Member
    And Delete Project Member
    Then The Project Member is removed from the Project Member list
